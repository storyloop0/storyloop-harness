"""Focused contract checks for the default one-call story engine."""

from scene_fixtures import FakeGenerator, a_turn

import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.agents.scene_turn import SceneContextProjector, SceneTurn, SingleSceneGenerator
from storyloop_harness.core.contracts import AgentContextEntry, Observation, WorldEvent
from storyloop_harness.core.open_actions import parse_mutable_fields, validate_open_effects
from storyloop_harness.core.contracts import Effect
from storyloop_harness.agents.action_advisor import ActionOption
from storyloop_harness.runtime.single_call import SingleCallGameSession
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.world.worldbook import Worldbook, WorldbookEntry


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "freeform"


class SingleCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = InMemoryGameStore()
        self.package = replace(ScenarioPackage.load(EXAMPLE), presentation_mode="novel")
        self.package.seed_game(self.store, "game")

    def session(self, generator, program=None):
        clock = StoryClock(8, overnight_requires_rest=True)
        projector = SceneContextProjector(self.store, self.package,
                                          clock=clock, program=program)
        return SingleCallGameSession(self.store, self.package, generator, projector,
                                     clock=clock)

    async def test_one_call_persists_npc_context_and_replay_uses_saved_plan(self):
        generator = FakeGenerator(a_turn())
        session = self.session(generator)

        first = await session.run_turn("game", "早上好", "turn-1")
        replay = await session.run_turn("game", "早上好", "turn-1")

        self.assertEqual(generator.calls, 1)
        self.assertEqual(first.narration, replay.narration)
        self.assertEqual([item.kind for item in first.segments], ["scene"])
        self.assertTrue(self.store.event_exists("game", "turn-1:input:reply:dockhand:spoken"))
        own_context = self.store.agent_context_entries("game", "dockhand")
        self.assertTrue(any("今天有船靠岸" in item.content for item in own_context))
        self.assertFalse(any(item.channel == "shared_experience" for item in own_context))
        self.assertEqual(len(session.proposed_options("game", "turn-1")), 3)

    async def test_notable_shared_moment_enters_only_participant_context(self):
        generator = FakeGenerator(a_turn(
            decision={"intent": "action", "target_ids": ["dockhand"]},
            prose="你和码头工一起把木箱搬到岸边，放下时相视一笑。",
            action={"status": "occurred", "player_result": "木箱搬到了岸边",
                    "sensory": "两人共同搬动木箱", "effects": []},
            memories=[
                {"actor_id": "dockhand", "fact": "第一天和玩家共同搬动木箱，放下时相视一笑。"},
                {"actor_id": "unknown", "fact": "没有在场的人不应获得这段经历。"},
            ],
        ))
        session = self.session(generator)

        first = await session.run_turn("game", "我和码头工一起搬木箱", "turn-1")
        await session.run_turn("game", "我和码头工一起搬木箱", "turn-1")

        memories = [item for item in self.store.observations_for("game", "dockhand")
                    if item.channel == "shared_experience"]
        self.assertEqual([item.content for item in memories],
                         ["第一天和玩家共同搬动木箱，放下时相视一笑。"])
        self.assertFalse(any(item.channel == "shared_experience"
                             for item in self.store.observations_for("game", "player")))
        self.assertTrue(any(item.channel == "shared_experience"
                            for item in self.store.agent_context_entries("game", "dockhand")))
        self.assertEqual(generator.calls, 1)
        self.assertEqual([item.kind for item in first.segments], ["scene"])

        unseen = FakeGenerator(a_turn(
            decision={"intent": "action", "target_ids": ["dockhand"]},
            prose="你独自走到岸边，放下随身的包。",
            action={"status": "occurred", "player_result": "你放下了包",
                    "sensory": "玩家放下随身的包", "effects": []},
            memories=[{"actor_id": "dockhand", "fact": "码头工夸奖了玩家的包。"}],
        ))
        await self.session(unseen).run_turn("game", "我独自到岸边放下包", "turn-2")
        self.assertEqual(len([item for item in self.store.observations_for("game", "dockhand")
                              if item.channel == "shared_experience"]), 1)

    async def test_interactive_mode_keeps_npc_reply_as_separate_visible_segment(self):
        package = replace(self.package, presentation_mode="interactive")
        generator = FakeGenerator(a_turn())
        clock = StoryClock(8, overnight_requires_rest=True)
        session = SingleCallGameSession(
            self.store, package, generator,
            SceneContextProjector(self.store, package, clock=clock), clock=clock,
        )

        outcome = await session.run_turn("game", "早上好", "turn-1")

        self.assertEqual([item.kind for item in outcome.segments], ["scene", "dialogue"])
        self.assertEqual(outcome.segments[1].speaker_id, "dockhand")
        self.assertEqual(generator.calls, 1)

    async def test_source_scene_does_not_commit_unprojected_reply_or_shared_memory(self):
        from scene_fixtures import source_package

        package = source_package()
        package.seed_game(self.store, "group")
        generator = FakeGenerator(a_turn(
            decision={"intent": "speech", "target_ids": ["dockhand", "guide"]},
            prose="Dockhand and Guide greet you.",
            replies=[{"actor_id": actor_id, "speech": "Welcome."}
                     for actor_id in ("dockhand", "guide")],
            memories=[{"actor_id": actor_id, "fact": "Greeted the visitor."}
                      for actor_id in ("dockhand", "guide")],
            story_first=True,
        ))
        session = SingleCallGameSession(self.store, package, generator,
                                         SceneContextProjector(self.store, package))

        outcome = await session.run_turn("group", "Hello everyone", "turn-group")

        self.assertEqual([part.speaker_id for part in outcome.segments if part.kind == "dialogue"],
                         ["dockhand"])
        self.assertFalse(self.store.event_exists("group", "turn-group:input:reply:guide:spoken"))
        self.assertFalse(any(item.channel == "shared_experience"
                             for item in self.store.observations_for("group", "guide")))
        self.assertTrue(any(item.channel == "shared_experience"
                            for item in self.store.observations_for("group", "dockhand")))

    async def test_actor_contexts_stay_separate_and_room_speech_is_observed(self):
        state = deepcopy(self.package.initial_state)
        state["actors"]["vendor"] = {"location": "harbor_square"}
        book = Worldbook(self.package.package_id, self.package.version,
                         [*self.package.worldbook._entries.values(),
                          WorldbookEntry("vendor_card", "摊主只知道自己的经历。", "actor",
                                         frozenset({"vendor"}), "card")])
        package = replace(self.package, initial_state=state,
                          actor_cards=(*self.package.actor_cards, ("vendor", "vendor_card")),
                          actor_names={**self.package.actor_names, "vendor": "摊主"},
                          worldbook=book)
        package.seed_game(self.store, "second")
        snapshot = self.store.load("second")
        self.store.commit("second", snapshot.version,
                          WorldEvent("vendor-secret", "private_fact", None, None, 0, ()),
                          (Observation("vendor-secret:seen", "vendor-secret", "vendor",
                                       "private_message", "只属于摊主的秘密", 0),), ())
        generator = FakeGenerator(a_turn(decision={"intent": "speech",
                                                  "target_ids": ["dockhand"],
                                                  "audience": "room"}))
        clock = StoryClock(8, overnight_requires_rest=True)
        session = SingleCallGameSession(
            self.store, package, generator,
            SceneContextProjector(self.store, package, clock=clock), clock=clock,
        )

        await session.run_turn("second", "大家好", "turn-1")

        request = generator.requests[0]
        self.assertNotIn("只属于摊主的秘密", str(request["player_history"]))
        dockhand_context = next(item for item in request["npc_contexts"]
                                if item["id"] == "dockhand")
        self.assertNotIn("只属于摊主的秘密", str(dockhand_context))
        self.assertTrue(any(item.content == "大家好"
                            for item in self.store.observations_for("second", "vendor")))
        self.assertTrue(any("码头工对玩家说" in item.content
                            for item in self.store.observations_for("second", "vendor")))

    async def test_invalid_action_effect_cannot_change_world(self):
        generator = FakeGenerator(a_turn(
            decision={"intent": "action", "target_ids": ["dockhand"]},
            action={"status": "occurred", "player_result": "窗户碎了",
                    "sensory": "玩家打破窗户", "effects": [
                        {"path": ["world", "shop_window", "broken"], "value": True}]},
        ))
        session = self.session(generator)

        result = await session.run_turn("game", "打破窗户", "turn-1")

        self.assertFalse(result.snapshot.data["world"]["shop_window"]["broken"])
        self.assertEqual(self.store.event_details("game", "turn-1:action")["outcome"], "attempted")
        self.assertEqual(generator.calls, 1)

    async def test_completed_activity_does_not_refill_old_scene_options(self):
        generator = FakeGenerator(a_turn(options=[]))
        session = self.session(generator)
        await session.run_turn("game", "我开始备菜", "turn-1")
        old_lead = ActionOption(label="开始备菜", input="我开始备菜。")

        self.assertEqual(session.proposed_options("game", "turn-1", (old_lead,)), ())

    async def test_projector_includes_player_identity_and_public_rules(self):
        book = Worldbook(self.package.package_id, self.package.version,
                         [*self.package.worldbook._entries.values(),
                          WorldbookEntry("filming_rule", "第一天只公布名字。", "public",
                                         frozenset(), "rule")])
        state = deepcopy(self.package.initial_state)
        state["player_profile"] = {"name": "林晚"}
        package = replace(self.package, worldbook=book, initial_state=state)
        package.seed_game(self.store, "profile-game")
        before = self.store.load("profile-game")
        self.store.commit("profile-game", before.version,
                          WorldEvent("recent-scene", "scene", None, None, 0, ()),
                          (Observation("recent-scene:player", "recent-scene", "player",
                                       "scene", "上午的光照在窗外雪坡上。", 0),), ())

        context = SceneContextProjector(self.store, package).project(
            self.store.load("profile-game"), "你好")

        self.assertEqual(context.request["player_profile"]["name"], "林晚")
        self.assertIn("第一天只公布名字。", context.request["public_rules"])
        self.assertIn("上午的光照在窗外雪坡上。", context.request["recent_visible_beats"])
        self.assertNotIn("avoid_repeated_scenery", context.request)

    async def test_older_relevant_memory_keeps_the_end_of_a_long_scene(self):
        entries = [AgentContextEntry(0, "old", "scene", "日常片段。" * 180 +
                                     "你和码头工约定第二天在港口见。", 0)]
        entries.extend(AgentContextEntry(index, f"recent-{index}", "scene",
                                         f"第{index}段新故事。", index)
                       for index in range(1, 14))

        recalled = SceneContextProjector._history(
            entries, "第二天的约定是什么", recent=10, older=1, excerpt_chars=700)

        self.assertIn("约定第二天在港口见", recalled[0]["text"])

    async def test_default_generation_keeps_story_first_with_optional_status(self):
        class CapturingModel:
            def __init__(self):
                self.schema = None
                self.calls = 0

            async def __call__(self, messages, *, structured_model):
                self.calls += 1
                self.schema = structured_model
                return SimpleNamespace(metadata={
                    "prose": "你和码头工做完饭，又坐下来吃了晚餐。",
                    "participants": ["dockhand"],
                    "interaction": "action",
                    "witnessed": "玩家和码头工一起做饭并吃了晚餐。",
                    "memories": [
                        {"actor_id": "dockhand", "fact": "一起做饭并吃了晚餐。"},
                        {"actor_id": "unknown", "fact": "不应保存。"},
                        {"bad": "sidecar"},
                    ],
                })

        model = CapturingModel()
        context = SceneContextProjector(self.store, self.package).project(
            self.store.load("game"), "我和码头工做饭并吃饭")
        self.assertNotIn("mutable_state", context.request)
        self.assertNotIn("updatable_status", context.request)
        self.assertNotIn("authored_action_leads", context.request)
        self.assertNotIn("recent_suggested_options", context.request)
        self.assertNotIn("avoid_repeated_scenery", context.request)

        session = self.session(SingleSceneGenerator(model, self.package))
        outcome = await session.run_turn("game", "我和码头工做饭并吃饭", "narrative-turn")
        turn = SceneTurn.model_validate(
            self.store.event_details("game", "narrative-turn:action")["single_call"])

        self.assertEqual(model.schema.__name__, "NarrativeTurn")
        self.assertEqual(model.calls, 1)
        self.assertEqual(set(model.schema.model_fields) - {"prose"},
                         {"replies", "options", "memories", "participants",
                          "interaction", "duration", "delivery", "witnessed", "status_changes"})
        self.assertEqual(turn.prose, "你和码头工做完饭，又坐下来吃了晚餐。")
        self.assertEqual(outcome.narration, turn.prose)
        self.assertEqual(turn.action.effects, [])
        self.assertTrue(any("一起做饭并吃了晚餐" in item.content
                            for item in self.store.observations_for("game", "dockhand")))
        self.assertFalse(any(item.channel == "shared_experience"
                             for item in self.store.observations_for("game", "unknown")))

    async def test_bad_action_sidecar_never_replaces_complete_story(self):
        prose = "你和码头工做完饭，端上桌，两人坐下吃了晚餐。"
        generator = FakeGenerator(a_turn(
            story_first=True,
            decision={"intent": "action", "target_ids": ["dockhand"]},
            prose=prose,
            action={"status": "occurred", "player_result": "只做好了饭",
                    "sensory": "玩家和码头工一起做饭并吃饭", "effects": [
                        {"path": ["world", "shop_window", "broken"], "value": True}]},
        ))

        outcome = await self.session(generator).run_turn(
            "game", "我和码头工做饭并吃饭", "turn-story-first")

        self.assertEqual(outcome.narration, prose)
        self.assertFalse(outcome.snapshot.data["world"]["shop_window"]["broken"])


    async def test_cancelled_generation_preserves_world_and_retry_commits_once(self):
        import asyncio
        entered, release = asyncio.Event(), asyncio.Event()
        generator = FakeGenerator(a_turn())
        original = generator.generate
        async def paused(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        generator.generate = paused
        session = self.session(generator)
        before = self.store.load('game')
        pending = self.store.pending_work('game')
        task = asyncio.create_task(session.run_turn('game', 'hello', 'cancelled'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.load('game'), before)
        self.assertEqual(self.store.pending_work('game'), pending)
        generator.generate = original
        result = await session.run_turn('game', 'hello', 'cancelled')
        replay = await session.run_turn('game', 'hello', 'cancelled')
        self.assertEqual(generator.calls, 1)
        self.assertEqual(result.snapshot, replay.snapshot)


    async def test_concurrent_retry_generates_and_delivers_speech_once(self):
        import asyncio
        entered, release, retry_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        generator = FakeGenerator(a_turn())
        original = generator.generate

        async def paused(*args):
            result = await original(*args)
            entered.set()
            await release.wait()
            return result

        generator.generate = paused
        self.package = replace(self.package, presentation_mode="interactive")
        session = self.session(generator)

        async def retry():
            retry_started.set()
            # No suspension before run_turn reaches its lock or the paused generator.
            return await session.run_turn("game", "hello", "same-turn")

        first_task = asyncio.create_task(session.run_turn("game", "hello", "same-turn"))
        replay_task = None
        try:
            await entered.wait()
            replay_task = asyncio.create_task(retry())
            await retry_started.wait()
            self.assertFalse(first_task.done())
            self.assertFalse(replay_task.done())
            self.assertEqual(generator.calls, 1)
        finally:
            release.set()
            tasks = [first_task] + ([replay_task] if replay_task is not None else [])
            await asyncio.gather(*tasks, return_exceptions=True)

        first, replay = first_task.result(), replay_task.result()
        self.assertEqual(generator.calls, 1)
        self.assertEqual(first.snapshot, replay.snapshot)
        self.assertEqual(first.narration, replay.narration)
        self.assertEqual(first.player_observations, replay.player_observations)
        replies = [entry for entry in self.store.agent_context_entries("game", "dockhand")
                   if entry.entry_id == "same-turn:input:reply:dockhand:spoken"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(first.processed_work_ids, ("same-turn:input:reply:dockhand",))
        self.assertEqual(replay.processed_work_ids, ())
        self.assertEqual([work.work_id for work in self.store.pending_work("game")],
                         ["harbor-opening-cue"])
        deliveries = [item for item in self.store.observations_for("game", "player")
                      if item.event_id == "same-turn:input:reply:dockhand:spoken"]
        self.assertEqual(len(deliveries), 1)

    async def test_failed_commit_leaves_character_history_unchanged_until_retry(self):
        from unittest.mock import patch
        generator = FakeGenerator(a_turn())
        session = self.session(generator)
        before = self.store.load("game")
        history = self.store.agent_context_entries("game", "dockhand")
        pending = self.store.pending_work("game")
        with patch.object(self.store, "commit", side_effect=RuntimeError("store unavailable")):
            with self.assertRaisesRegex(RuntimeError, "store unavailable"):
                await session.run_turn("game", "hello", "retry-commit")
        self.assertEqual(self.store.load("game"), before)
        self.assertEqual(self.store.agent_context_entries("game", "dockhand"), history)
        self.assertEqual(self.store.pending_work("game"), pending)
        await session.run_turn("game", "hello", "retry-commit")
        self.assertEqual(generator.calls, 2)
        self.assertTrue(self.store.event_exists("game", "retry-commit:input:reply:dockhand:spoken"))


class ActivityTransitionTests(unittest.TestCase):
    def test_declared_activity_rejects_skipping_stages(self):
        state = {"world": {"meal_phase": "preparing"}}
        fields = parse_mutable_fields([{
            "path": ["world", "meal_phase"],
            "values": ["preparing", "cooking", "served"],
            "transitions": [
                {"from": "preparing", "to": ["cooking"]},
                {"from": "cooking", "to": ["served"]},
            ],
        }], state)

        self.assertEqual(fields[0].next_values("preparing"), ("cooking",))
        validate_open_effects((Effect(("world", "meal_phase"), "cooking"),), fields, state)
        with self.assertRaises(ValueError):
            validate_open_effects((Effect(("world", "meal_phase"), "served"),), fields, state)


if __name__ == "__main__":
    unittest.main()
