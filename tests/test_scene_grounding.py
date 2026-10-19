"""Grounding and dialogue instructions at the actual scene model boundary."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scene_fixtures import source_package
from scene_fixtures import FakeGenerator, a_turn
from storyloop_harness.agents.scene_turn import SingleSceneGenerator
from storyloop_harness.context.budget import estimate_scene_request
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.runtime.single_call import SingleCallGameSession
from storyloop_harness.usage import collect_usage, record_model_usage
from storyloop_harness.world.scenario import ScenarioPackage


FREEFORM = Path(__file__).resolve().parents[1] / "examples" / "freeform"


class CaptureModel:
    def __init__(self):
        self.messages = None
        self.kwargs = None

    async def __call__(self, messages, **kwargs):
        self.messages, self.kwargs = messages, kwargs
        return SimpleNamespace(metadata={
            "beats": [{"kind": "narration", "text": "码头工放下缆绳。"}],
            "options": [
                {"label": "看看码头", "input": "我看看码头。"},
                {"label": "询问工人", "input": "我询问工人。"},
                {"label": "沿岸走", "input": "我沿岸走。"},
            ],
        })


@pytest.mark.parametrize("source", [False, True], ids=["freeform", "source"])
@pytest.mark.parametrize("mode", ["interactive", "novel"])
def test_scene_generation_sends_mode_specific_grounding_and_budget(source, mode):
    package = source_package() if source else ScenarioPackage.load(FREEFORM)
    package = replace(package, presentation_mode=mode)
    store = InMemoryGameStore()
    package.seed_game(store, "game")
    context = SceneContextProjector(store, package).project(
        store.load("game"), "码头工，今夜风向确认了吗？")
    model = CaptureModel()

    asyncio.run(SingleSceneGenerator(model, package).generate("game", context))

    content = next(item["content"] for item in model.messages if item["role"] == "system")
    system = "".join(part["text"] for part in content) if isinstance(content, list) else content
    assert model.kwargs["context_budget"] == context.budget
    assert model.kwargs["max_completion_tokens"] == context.budget.output_tokens
    assert (estimate_scene_request(context.request, package, context.budget).system_tokens
            == context.report.system_tokens)
    assert "提问本身" in system and "职业" in system and "常识" in system
    assert "编造观察" in system and "传闻" in system
    assert "不知道" in system and "推测" in system
    assert all(field in system for field in ("beats", "witnessed", "memories", "options"))
    assert "npc_contexts" in system and "自己的经历" in system
    assert "每段描写至少要揭示新事实" not in system
    assert "其他字段仅供保存上下文" not in system
    if mode == "interactive":
        assert "interactive 模式" in system and "novel 模式" not in system
        assert "不引用或转述同一回答" in system
        assert "有序 beats" in system
        assert "正文对白逐一摘录" not in system
    else:
        assert "novel 模式" in system and "interactive 模式" not in system
        assert "完整场景" in system and "角色记忆" in system
        assert "正文对白逐一摘录" not in system
    assert system.endswith("输出 SourceNarrativeTurn。" if source else "输出 NarrativeTurn。")
    if source:
        assert "player_identity_knowledge" in system


@pytest.mark.parametrize("mode", ["interactive", "novel"])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("material,question,clue", [
    ("黄铜钥匙 brasskey731 交给守灯人阿禾", "brasskey731 黄铜钥匙交给谁？", "守灯人阿禾"),
    ("蓝帆船 sailmarker824 停在东侧栈桥", "sailmarker824 蓝帆船停在哪边？", "东侧栈桥"),
])
def test_narrative_fixture_keeps_player_recollection_out_of_dockhand_reply(
        mode, known, material, question, clue):
    import json
    from examples.policy_comparison.narrative import NarrativeFixture

    request = {"player_history": [], "player_summary": material if known else "",
               "presentation_mode": mode, "player_input": question}
    response = asyncio.run(NarrativeFixture()([
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]))
    result = response.metadata
    speech = result["beats"][1]["text"]
    assert result["participants"] == ["dockhand"]
    assert result["beats"][1] == {"kind": "dialogue", "actor_id": "dockhand", "text": speech}
    assert clue not in speech
    assert "线索不足" in speech
    assert ("你记得" in result["beats"][0]["text"] and clue in result["beats"][0]["text"]) is known
    assert "码头工停下手里的活" in result["beats"][0]["text"]
    assert speech not in result["beats"][0]["text"]


@pytest.mark.parametrize("mode", ["interactive", "novel"])
def test_sqlite_replay_keeps_two_identical_speakers_distinct(mode, tmp_path):
    package = replace(source_package(), story_blueprint=None, presentation_mode=mode)
    path = tmp_path / "scene.db"
    speech = "风向还没有确认。"
    prose = "码头工和摊主放下手里的活。"
    turn = a_turn(
        decision={"intent": "speech", "target_ids": ["dockhand", "vendor"],
                  "audience": "room"},
        prose=prose,
        replies=[{"actor_id": actor, "speech": speech}
                 for actor in ("dockhand", "vendor")],
    )
    class MeteredGenerator(FakeGenerator):
        async def generate(self, game_id, context):
            result = await super().generate(game_id, context)
            record_model_usage("offline-scene-test", "scene_turn",
                               SimpleNamespace(input_tokens=3, output_tokens=2))
            return result

    generator = MeteredGenerator(turn)
    with SQLiteGameStore(path) as store:
        package.seed_game(store, "game")
        session = SingleCallGameSession(
            store, package, generator, SceneContextProjector(store, package))
        with collect_usage() as fresh_usage:
            first = asyncio.run(session.run_turn("game", "两位怎么看风向？", "turn"))
        assert len(fresh_usage.records) == 1
        assert first.narration.count(speech) == 2
        assert [item.speaker_id for item in first.segments if item.kind == "dialogue"] == (
            ["dockhand", "vendor"] if mode == "interactive" else [])
        for actor in ("dockhand", "vendor"):
            assert store.event_exists("game", f"turn:input:reply:{actor}:spoken")
            assert any(item.entry_id == f"turn:input:reply:{actor}:spoken"
                       and speech in item.content
                       for item in store.agent_context_entries("game", actor))
        assert sum(speech in item.content for item in store.observations_for("game", "guard")
                   if item.channel == "overheard") == 2
        version = store.load("game").version
        player_observations = tuple(store.observations_for("game", "player"))
    with SQLiteGameStore(path) as store:
        with collect_usage() as replay_usage:
            replay = asyncio.run(SingleCallGameSession(
                store, package, generator, SceneContextProjector(store, package)).run_turn(
                    "game", "两位怎么看风向？", "turn"))
        assert replay_usage.records == []
        assert generator.calls == 1
        assert replay.narration == first.narration
        assert replay.processed_work_ids == ()
        assert store.load("game").version == version
        assert tuple(store.observations_for("game", "player")) == player_observations


def test_private_reply_is_not_overheard(tmp_path):
    package = replace(source_package(), story_blueprint=None,
                      presentation_mode="interactive")
    speech = "还没有确认。"
    turn = a_turn(decision={"intent": "speech", "target_ids": ["dockhand"],
                            "channel": "private_message"},
                  replies=[{"actor_id": "dockhand", "speech": speech}])
    with SQLiteGameStore(tmp_path / "private.db") as store:
        package.seed_game(store, "game")
        outcome = asyncio.run(SingleCallGameSession(
            store, package, FakeGenerator(turn), SceneContextProjector(store, package)
        ).run_turn("game", "悄悄问码头工", "turn"))
        assert outcome.narration.count(speech) == 1
        assert store.event_exists("game", "turn:input:reply:dockhand:spoken")
        assert not any(item.channel == "overheard" for item in
                       store.observations_for("game", "guard"))
