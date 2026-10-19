"""One-model-call ordinary turns with independently persisted NPC histories."""

from __future__ import annotations

import asyncio
import re
from weakref import WeakValueDictionary

from storyloop_harness.core.store_port import GameStore
from storyloop_harness.context import ContextBudgetExceeded
from storyloop_harness.context.metrics import record_projection_metrics
from storyloop_harness.adapters.telemetry import NoopTelemetry, Telemetry, session_id_for_game
from storyloop_harness.agents.action_advisor import ActionOption
from storyloop_harness.core.decisions import MainDecision
from storyloop_harness.agents.scene_turn import SingleSceneGenerator
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.agents.scene_schema import NarrationBeat, SceneTurn, committed_scene
from storyloop_harness.runtime.action_resolution import resolve_action
from storyloop_harness.core.contracts import Effect, Observation, PendingWork, Snapshot, WorldEvent
from storyloop_harness.core.perception import physical_observations
from storyloop_harness.core.turn_result import TurnOutcome
from storyloop_harness.runtime.player_knowledge import accepted_encounters, merge_knowledge
from storyloop_harness.core.turn_result import StorySegment
from storyloop_harness.runtime.presentation import segment_for_observation
from storyloop_harness.runtime.runner import RunResult, TurnRunner, WorkResult
from storyloop_harness.runtime.schedule import scenario_cue
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.runtime.turn_progress import TurnProgress, emit
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.world.facts import FactQuery
from storyloop_harness.world.status_fields import status_effects
from storyloop_harness.agents.scene_prompt import scene_system_prompt
from storyloop_harness.agents.scene_schema import scene_output_schema
from storyloop_harness.runtime.player_preferences import current_player_preferences
from storyloop_harness.models.usage import capture_success_usage
from storyloop_harness.core.generated_scene import (
    GeneratedSceneRecovery, GeneratedSceneSnapshot, GeneratedSceneRecoveryInspection,
    GeneratedSceneGenerationMarker, GeneratedSceneUncertain,
    GeneratedSceneMismatch,
    GeneratedSceneStale, GeneratedSceneCorrupt, GeneratedSceneWriteFailure,
    GeneratedSceneConflict,
    content_hash, text_hash, package_fingerprint, snapshot_state_fingerprint, logical_value,
    validate_event_snapshot_reference, GENERATION_CONTRACT_VERSION,
)


def validated_options(raw: list[dict], recent: tuple[str, ...] = ()) -> tuple[ActionOption, ...]:
    """Keep only distinct generated actions; stale scene leads cannot refill a turn."""
    selected: list[ActionOption] = []
    used = {item.strip() for item in recent}
    for candidate in raw:
        try:
            item = ActionOption.model_validate(candidate)
        except Exception:
            continue
        if (item.input.strip() in used
                or re.search(r"/(?:choose|next|rest|continue)\b", item.input + item.label, re.I)
                or any(prior.input == item.input or prior.label == item.label for prior in selected)):
            continue
        selected.append(item)
        if len(selected) == 3:
            break
    return tuple(selected)


class SingleCallGameSession:
    """Generate once, commit authoritative facts, and replay NPC speech without a model."""

    def __init__(self, store: GameStore, package: ScenarioPackage,
                 generator: SingleSceneGenerator, projector: SceneContextProjector,
                 *, clock: StoryClock | None = None, max_steps: int = 8,
                 telemetry: Telemetry | None = None,
                 generated_scene_recovery: GeneratedSceneRecovery | None = None) -> None:
        self.store = store
        self.package = package
        self.generator = generator
        self.projector = projector
        self.clock = clock
        self.max_steps = max_steps
        self.telemetry = telemetry or NoopTelemetry()
        self.generated_scene_recovery = generated_scene_recovery
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _bindings(self, before: Snapshot, input_hash: str, max_tick: int | None) -> dict[str, object]:
        projector = self.projector
        return dict(input_hash=input_hash, base_version=before.version,
            base_tick=before.tick, base_state_hash=snapshot_state_fingerprint(before),
            scenario_id=self.package.package_id, scenario_version=self.package.version,
            package_fingerprint=package_fingerprint(self.package),
            system_prompt_hash=text_hash(scene_system_prompt(self.package)),
            output_schema_hash=content_hash(scene_output_schema(self.package).model_json_schema()),
            commit_config_hash=content_hash(dict(clock=logical_value(self.clock),
                max_tick=max_tick, presentation_mode=self.package.presentation_mode,
                package_rules=package_fingerprint(self.package))),
            generation_config_hash=content_hash(dict(
                context_policy=projector.context_policy.value,
                context_spec=logical_value(projector.policy_spec),
                context_budget=logical_value(projector.context_budget),
                max_responders=projector.max_responders,
                generation_identity=self.generated_scene_recovery.generation_identity)))

    def _check_snapshot(self, record: GeneratedSceneSnapshot, before: Snapshot,
                        input_hash: str, max_tick: int | None, *, committed: bool = False) -> None:
        body = record.metadata()
        legacy = body['generation_contract_version'] != GENERATION_CONTRACT_VERSION
        if legacy and not committed:
            raise GeneratedSceneMismatch('legacy generated scene cannot be committed')
        expected = self._bindings(before, input_hash, max_tick)
        for key, value in expected.items():
            if committed and key in ('base_version', 'base_tick', 'base_state_hash'):
                continue
            if legacy and committed and key in ('system_prompt_hash', 'output_schema_hash'):
                continue
            if body[key] != value:
                if key in ('base_version', 'base_tick', 'base_state_hash'):
                    raise GeneratedSceneStale('generated scene base state changed')
                raise GeneratedSceneMismatch('generated scene binding changed')

    def _saved_turn(self, game_id: str, turn_id: str):
        event_id = f'{turn_id}:input'
        for kind in ('input', 'query', 'action'):
            candidate = f'{turn_id}:{kind}'
            if self.store.event_exists(game_id, candidate):
                event_id = candidate
                break
        return event_id, self.store.event_details(game_id, event_id)

    def _check_generation_marker(self, marker: GeneratedSceneGenerationMarker,
                                 before: Snapshot, turn_id: str, input_hash: str,
                                 max_tick: int | None) -> None:
        if not isinstance(marker, GeneratedSceneGenerationMarker):
            raise GeneratedSceneCorrupt('generated scene generation marker wrapper identity mismatch')
        validated = GeneratedSceneGenerationMarker.from_storage(marker.canonical_json, marker.checksum)
        if (validated != marker or marker.game_id != before.game_id or marker.turn_id != turn_id):
            raise GeneratedSceneCorrupt('generated scene generation marker wrapper identity mismatch')
        body = validated.metadata()
        for key, value in self._bindings(before, input_hash, max_tick).items():
            if body[key] != value:
                if key in ('base_version', 'base_tick', 'base_state_hash'):
                    raise GeneratedSceneStale('generated scene base state changed')
                raise GeneratedSceneMismatch('generated scene binding changed')

    def _inspect_uncertain_generation(self, before: Snapshot, turn_id: str,
                                      input_hash: str, max_tick: int | None) -> None:
        recovery = self.generated_scene_recovery
        if recovery is None or not recovery.protect_uncertain_generation:
            return
        marker = recovery.snapshot_store.generated_scene_generation_marker(before.game_id, turn_id)
        if marker is not None:
            self._check_generation_marker(marker, before, turn_id, input_hash, max_tick)
            raise GeneratedSceneUncertain()

    def _claim_generation(self, before: Snapshot, turn_id: str,
                          input_hash: str, max_tick: int | None) -> None:
        recovery = self.generated_scene_recovery
        if recovery is None or not recovery.protect_uncertain_generation:
            return
        marker = GeneratedSceneGenerationMarker.create(game_id=before.game_id, turn_id=turn_id,
            **self._bindings(before, input_hash, max_tick))
        try:
            claimed = recovery.snapshot_store.create_generated_scene_generation_marker(marker)
        except (GeneratedSceneCorrupt, GeneratedSceneConflict,
                GeneratedSceneMismatch, GeneratedSceneStale):
            raise
        except Exception as exc:
            raise GeneratedSceneWriteFailure('generated scene generation marker save failed') from exc
        if claimed is True:
            return
        if claimed is not False:
            raise GeneratedSceneWriteFailure('generated scene generation marker save failed')
        prior = recovery.snapshot_store.generated_scene_generation_marker(before.game_id, turn_id)
        if prior is None:
            raise GeneratedSceneCorrupt('generated scene generation marker missing after occupied claim')
        self._check_generation_marker(prior, before, turn_id, input_hash, max_tick)
        raise GeneratedSceneUncertain()

    def inspect_generated_scene_recovery(
            self, before: Snapshot, turn_id: str, input_hash: str,
            max_tick: int | None = None) -> GeneratedSceneRecoveryInspection | None:
        if self.generated_scene_recovery is None:
            raise ValueError('generated scene recovery is not configured')
        record = self.generated_scene_recovery.snapshot_store.generated_scene_snapshot(
            before.game_id, turn_id)
        event_id, saved = self._saved_turn(before.game_id, turn_id)
        referenced = saved is not None and any(key in saved for key in (
            'generated_scene_snapshot_id', 'generated_scene_snapshot_checksum'))
        if record is None:
            if referenced:
                raise GeneratedSceneCorrupt('generated scene event reference missing snapshot')
            if self.generated_scene_recovery.protect_uncertain_generation:
                if saved is not None:
                    if (not isinstance(saved.get('text'), str)
                            or text_hash(saved['text']) != input_hash
                            or not isinstance(saved.get('single_call'), dict)):
                        raise ValueError('turn ID already belongs to another action')
                    committed_scene(saved['single_call'])
                else:
                    self._inspect_uncertain_generation(before, turn_id, input_hash, max_tick)
            return None
        validated = GeneratedSceneSnapshot.from_storage(record.canonical_json, record.checksum)
        if validated != record or record.game_id != before.game_id or record.turn_id != turn_id:
            raise GeneratedSceneCorrupt('generated scene wrapper identity mismatch')
        if saved is not None:
            if not referenced:
                raise GeneratedSceneCorrupt('generated scene event reference missing')
            reference = WorldEvent(event_id, 'player_input', 'player', None,
                                   before.tick, (), saved)
            validate_event_snapshot_reference(reference, record)
        self._check_snapshot(record, before, input_hash, max_tick, committed=saved is not None)
        return GeneratedSceneRecoveryInspection(record, saved is not None)

    def _npc_reply(self, snapshot: Snapshot, work: PendingWork) -> WorkResult:
        actor_id = work.payload["actor_id"]
        speech = work.payload["speech"]
        if not isinstance(actor_id, str) or actor_id not in self.package.actor_names:
            raise ValueError("single-call reply has unknown actor")
        if not isinstance(speech, str) or not speech.strip():
            raise ValueError("single-call reply is empty")
        profiles = snapshot.data.get("actor_profiles", {})
        profile = profiles.get(actor_id, {}) if isinstance(profiles, dict) else {}
        name = (profile.get("name") if isinstance(profile, dict)
                and isinstance(profile.get("name"), str) else self.package.actor_names[actor_id])
        knowledge = snapshot.data.get("player_knowledge")
        named = knowledge.get("named_actor_ids", []) if isinstance(knowledge, dict) else []
        player_label = (name if not isinstance(knowledge, dict) or actor_id in named
                        else "一位嘉宾")
        event = WorldEvent(f"{work.work_id}:spoken", "npc_spoke", actor_id,
                           work.cause_id, snapshot.tick, (),
                           {"player_message": work.payload.get("player_message", ""),
                            "speech": speech, "speaker_id": actor_id,
                            "speaker_name": player_label})
        observations: list[Observation] = []
        if self.package.presentation_mode == "interactive" and not work.payload.get('visible_committed'):
            observations.append(Observation(f"{event.event_id}:player-heard", event.event_id,
                                            "player", "dialogue", f"【{player_label}】\n{speech}", snapshot.tick))
        if not work.payload.get("private", False):
            actors = snapshot.data.get("actors", {})
            speaker_state = actors.get(actor_id, {}) if isinstance(actors, dict) else {}
            location = speaker_state.get("location") if isinstance(speaker_state, dict) else None
            for listener_id, state in actors.items():
                if (listener_id not in {"player", actor_id} and isinstance(state, dict)
                        and state.get("location") == location):
                    observations.append(Observation(
                        f"{event.event_id}:overheard:{listener_id}", event.event_id,
                        listener_id, "overheard", f"{name}对玩家说：{speech}", snapshot.tick,
                    ))
        return WorkResult(event, tuple(observations), ())

    def _runner(self) -> TurnRunner:
        handlers = {"single_npc_reply": self._npc_reply, "scenario_cue": scenario_cue}
        return TurnRunner(self.store, handlers,
                          self.max_steps, telemetry=self.telemetry)

    def _reject_legacy_work(self, game_id: str) -> None:
        if any(work.kind == "npc_reply" for work in self.store.pending_work(game_id)):
            raise ValueError("Unsupported legacy save contains npc_reply work; start a new game.")

    async def run_ready_work(self, game_id: str,
                             progress: TurnProgress | None = None) -> RunResult:
        async with self._locks.setdefault(game_id, asyncio.Lock()):
            self._reject_legacy_work(game_id)
            return await self._runner().run_async(game_id)

    async def run_turn(self, game_id: str, player_text: str, turn_id: str,
                       progress: TurnProgress | None = None,
                       max_tick: int | None = None) -> TurnOutcome:
        return await self.run_turn_bounded(game_id, player_text, turn_id,
                                           max_tick=max_tick, progress=progress)

    async def run_fact_query(self, game_id: str, query: FactQuery, turn_id: str,
                             progress: TurnProgress | None = None) -> TurnOutcome:
        from storyloop_harness.runtime.fact_query import run_fact_query
        return await run_fact_query(self, game_id, query, turn_id, progress)

    async def run_turn_bounded(self, game_id: str, player_text: str, turn_id: str,
                               *, max_tick: int | None,
                               progress: TurnProgress | None = None) -> TurnOutcome:
        if not player_text.strip() or not turn_id:
            raise ValueError("turn requires text and ID")
        async with self._locks.setdefault(game_id, asyncio.Lock()):
            self._reject_legacy_work(game_id)
            visible_before = {item.observation_id
                              for item in self.store.observations_for(game_id, "player")}
            _, saved = self._saved_turn(game_id, turn_id)
            if isinstance(saved, dict) and 'fact_query' in saved:
                raise ValueError('turn ID already belongs to a fact query')
            before = self.store.load(game_id)
            inspection = (self.inspect_generated_scene_recovery(
                before, turn_id, text_hash(player_text), max_tick)
                if self.generated_scene_recovery is not None else None)
            if saved is not None:
                if saved.get("text") != player_text or not isinstance(saved.get("single_call"), dict):
                    raise ValueError("turn ID already belongs to another action")
                plan = committed_scene(saved["single_call"])
            else:
                record = inspection.snapshot if inspection is not None else None
                if record is not None:
                    plan = record.scene()
                    await emit(progress, 'stage', stage='committing')
                    before = self._commit_player(before, turn_id, player_text, plan, max_tick, record)
                else:
                    await emit(progress, "stage", stage="thinking")
                    try:
                        context = self.projector.project(before, player_text)
                    except ContextBudgetExceeded as error:
                        with self.telemetry.span('single-scene-projection',
                                {'game_id': game_id, 'turn_id': turn_id},
                                session_id=session_id_for_game(game_id, self.package.package_id)) as span:
                            record_projection_metrics(span, error.report)
                            span.metric('story.context.rejected', 1.0)
                        raise
                    with self.telemetry.span(
                        "single-call-turn", {"game_id": game_id, "turn_id": turn_id,
                                              "focus_actor_ids": list(context.focus_actor_ids)},
                        session_id=session_id_for_game(game_id, self.package.package_id),
                    ) as span:
                        self._claim_generation(before, turn_id, text_hash(player_text), max_tick)
                        if self.generated_scene_recovery is not None:
                            with capture_success_usage() as success:
                                plan = await self.generator.generate(game_id, context)
                        else:
                            plan = await self.generator.generate(game_id, context)
                        plan = self._validate_plan(before, plan, context.focus_actor_ids,
                                                   context.nearby_actor_ids)
                        span.metric("story.turn_model_calls", 1.0)
                    if self.generated_scene_recovery is not None:
                        record = GeneratedSceneSnapshot.create(
                            game_id=game_id, turn_id=turn_id,
                            **self._bindings(before, text_hash(player_text), max_tick),
                            projected_request_hash=content_hash(context.request),
                            focus_actor_ids=context.focus_actor_ids,
                            nearby_actor_ids=context.nearby_actor_ids,
                            preference_hash=content_hash(current_player_preferences()),
                            scene=plan, success_usage=tuple(success.records),
                            usage_coverage=success.coverage)
                        try:
                            record = self.generated_scene_recovery.snapshot_store.save_generated_scene_snapshot(record)
                        except GeneratedSceneConflict:
                            raise
                        except Exception as exc:
                            raise GeneratedSceneWriteFailure('generated scene save failed') from exc
                    await emit(progress, "stage", stage="committing")
                    before = self._commit_player(before, turn_id, player_text, plan, max_tick, record)
            return await self._finish_turn(game_id, turn_id, plan, visible_before, progress)

    async def _finish_turn(self, game_id, turn_id, plan, visible_before, progress,
                           *, processed_before=()) -> TurnOutcome:
        await emit(progress, "stage", stage="characters")
        processed = await self._runner().run_async(game_id)
        observations = tuple(item for item in self.store.observations_for(game_id, "player")
                             if (item.observation_id not in visible_before
                                 or item.event_id.startswith(f"{turn_id}:")))
        segments = tuple(segment_for_observation(self.store, game_id, item)
                         for item in observations)
        if not segments:
            segments = (StorySegment("narration", plan.prose),)
        narration = "\n\n".join(item.body_text for item in segments)
        for segment in segments:
            await emit(progress, "segment", segment=segment.to_dict())
        return TurnOutcome(plan.decision, narration, observations,
                           processed_before + processed.processed_work_ids, processed.snapshot,
                           False, segments, remaining_work_ids=tuple(
                               work.work_id for work in self.store.ready_work(
                                   game_id, processed.snapshot.tick)))

    def _validate_plan(self, before: Snapshot, plan: SceneTurn,
                       focus: tuple[str, ...], nearby: tuple[str, ...]) -> SceneTurn:
        # Custom generators and model_copy/model_construct callers must cross the
        # same validation boundary before any snapshot or event is persisted.
        plan = SceneTurn.model_validate(plan.model_dump())
        plan = resolve_action(before, self.package, plan)
        if plan.decision.intent == 'inspect' and not plan.story_first and plan.decision.entry_id:
            entry = self.package.worldbook.get(plan.decision.entry_id, 'player')
            if entry is not None:
                plan = plan.model_copy(update={'beats': [NarrationBeat(kind='narration', text=entry.text)]})
        # Reject impossible numeric proposals before persisting their prose.
        status_effects(self.package.status_fields, before,
                       [change.model_dump() for change in plan.status_changes])
        decision = plan.decision
        allowed = set(focus)
        profiles = before.data.get("actor_profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
        def actor_name(actor_id: str) -> str:
            profile = profiles.get(actor_id)
            return (profile.get("name") if isinstance(profile, dict)
                    and isinstance(profile.get("name"), str) else
                    self.package.actor_names.get(actor_id, ""))
        targets = list(dict.fromkeys(actor_id for actor_id in decision.target_ids
                                     if actor_id in allowed))[:len(allowed)]
        replies = plan.replies
        if decision.intent in {"speech", "action"} and not targets:
            targets = list(dict.fromkeys(item.actor_id for item in replies))
        if decision.intent == "action" or (decision.intent == "speech" and decision.channel == "speech"):
            targets = [actor_id for actor_id in targets if actor_id in nearby]
        if decision.channel == "private_message" and len(targets) > 1:
            targets = targets[:1]
        if any(item.actor_id not in allowed or item.actor_id not in targets for item in replies):
            raise ValueError('dialogue actor is not authorized for this turn')
        decision = decision.model_copy(update={"target_ids": targets})
        milestones = []
        if self.package.story_blueprint is not None:
            progress = before.data.get("story_progress", {})
            completed = progress.get("completed", {}) if isinstance(progress, dict) else {}
            day = (before.tick // self.clock.ticks_per_day + 1 if self.clock else 1)
            active = {item["id"] for item in self.package.story_blueprint.active_milestones(
                day, completed if isinstance(completed, dict) else {},
            )}
            milestones = [item for item in dict.fromkeys(plan.milestones_fulfilled)
                          if item in active]
        prose = plan.prose.strip()
        encounters = (accepted_encounters(
            plan.encounters, prose,
            {actor_id: actor_name(actor_id) for actor_id in allowed},
        ) if self.package.story_blueprint is not None else [])
        memorable = (decision.intent == "speech" or
                     (decision.intent == "action" and
                      (plan.story_first or plan.action.status == "occurred")))
        participants = set(targets) & set(nearby) if memorable else set()
        memories = []
        for item in plan.memories:
            visibly_present = (
                actor_name(item.actor_id) in prose
                or any(reply.actor_id == item.actor_id for reply in replies)
            ) if item.actor_id in participants else False
            if visibly_present and item.actor_id not in {
                prior.actor_id for prior in memories
            } and item.fact.strip():
                memories.append(item.model_copy(update={"fact": item.fact.strip()[:180]}))
            if len(memories) == (len(allowed) if self.package.story_blueprint else 2):
                break
        return plan.model_copy(update={"decision": decision, "memories": memories,
                                       "encounters": encounters,
                                       "milestones_fulfilled": milestones})

    def _commit_player(self, before: Snapshot, turn_id: str, player_text: str,
                       plan: SceneTurn, max_tick: int | None,
                       generated_scene: GeneratedSceneSnapshot | None = None,
                       *, fact_query: dict[str, object] | None = None) -> Snapshot:
        decision = plan.decision
        duration_ticks = self.clock.elapsed(decision.duration, before.tick) if self.clock else 1
        if max_tick is not None:
            duration_ticks = min(duration_ticks, max(0, max_tick - before.tick))
        tick = before.tick + duration_ticks
        actors = before.data.get("actors", {})
        player = actors.get("player", {}) if isinstance(actors, dict) else {}
        location = player.get("location") if isinstance(player, dict) else None
        kind = {"speech": "input", "inspect": "query", "action": "action"}[decision.intent]
        event_id = f"{turn_id}:{kind}"
        details: dict[str, object] = {
            "text": player_text, "request_text": player_text,
            "before_tick": before.tick, "duration_ticks": duration_ticks,
            "duration": decision.duration, "target_ids": list(decision.target_ids),
            "channel": decision.channel, "audience": decision.audience,
            "single_call": plan.model_dump(),
        }
        if generated_scene is not None:
            details['generated_scene_snapshot_id'] = generated_scene.snapshot_id
            details['generated_scene_snapshot_checksum'] = generated_scene.checksum
        if fact_query is not None:
            details['fact_query'] = fact_query
        effects: tuple[Effect, ...] = ()
        if decision.intent == "action":
            accepted = plan.action.status == "occurred"
            if accepted and decision.action_id is not None:
                # resolve_action already validated this rule against the current snapshot.
                effects = self.package.action_rules[decision.action_id].effects
            elif accepted:
                effects = tuple(Effect(tuple(item.path), item.value) for item in plan.action.effects)
            details.update({"action_id": decision.action_id,
                            "outcome": "accepted" if accepted else "rejected",
                            "location": location, "sensory": plan.action.sensory,
                            "player_result": plan.action.player_result})
            if not accepted:
                details["reason"] = plan.action.player_result
        elif decision.intent == "inspect":
            entry = (self.package.worldbook.get(decision.entry_id, "player")
                     if decision.entry_id else None)
            if entry is not None:
                details["entry_id"] = entry.entry_id
        if self.package.story_blueprint is not None and plan.milestones_fulfilled:
            effects += tuple(Effect(("story_progress", "completed", milestone_id), True)
                             for milestone_id in plan.milestones_fulfilled)
            if any(step.terminal and step.id in plan.milestones_fulfilled
                   for step in self.package.story_blueprint.milestones):
                effects += (Effect(("story_progress", "complete"), True),)
            details["milestones_fulfilled"] = plan.milestones_fulfilled
        knowledge = before.data.get("player_knowledge")
        if isinstance(knowledge, dict) and plan.encounters:
            updated_knowledge = merge_knowledge(knowledge, plan.encounters)
            if updated_knowledge != knowledge:
                effects += (Effect(("player_knowledge",), updated_knowledge),)
                details["player_encounters"] = [item.model_dump() for item in plan.encounters]
        effects += status_effects(self.package.status_fields, before,
                                  [change.model_dump() for change in plan.status_changes])
        event = WorldEvent(event_id, {"speech": "player_input",
                                      "inspect": "player_query",
                                      "action": "player_action" if plan.action.status == "occurred"
                                      else "action_rejected"}[decision.intent],
                           "player", None, tick, effects, details)
        # Player observations are written in beat order atomically. Pending NPC
        # work owns history/overhearing only, so resume cannot append or reorder
        # the already visible dialogue. Metadata is indexed per observation.
        visible_segments = []
        profiles = before.data.get('actor_profiles', {})
        knowledge = before.data.get('player_knowledge')
        named = set(knowledge.get('named_actor_ids', [])) if isinstance(knowledge, dict) else set()
        for beat in plan.beats:
            if beat.kind == 'narration':
                visible_segments.append(StorySegment('scene', beat.text))
            else:
                profile = profiles.get(beat.actor_id, {}) if isinstance(profiles, dict) else {}
                name = profile.get('name') if isinstance(profile, dict) else None
                name = name if isinstance(name, str) else self.package.actor_names[beat.actor_id]
                label = name if not isinstance(knowledge, dict) or beat.actor_id in named else '一位嘉宾'
                visible_segments.append(StorySegment('dialogue', beat.text, beat.actor_id, label))
            # An accepted introduction changes the label only after the beat
            # containing its visible evidence. Evidence spanning beats has no
            # unambiguous reveal point within a beat, so stays hidden this turn.
            named.update(item.actor_id for item in plan.encounters
                         if item.name_learned and item.evidence in beat.text)
        if self.package.presentation_mode == 'novel':
            if (fact_query is not None and len(visible_segments) == 1
                    and visible_segments[0].kind == 'dialogue'):
                details['fact_query_novel_speaker_name'] = visible_segments[0].speaker_name
            visible_segments = [StorySegment('scene', '\n\n'.join(
                f'{part.speaker_name}说：“{part.text}”' if part.kind == 'dialogue' else part.text
                for part in visible_segments))]
        observations: list[Observation] = []
        details['visible_beats'] = {}
        for index, part in enumerate(visible_segments):
            observation_id = f'{event_id}:beat:{index}'
            observations.append(Observation(observation_id, event_id, 'player',
                part.kind, part.body_text, tick))
            details['visible_beats'][observation_id] = part.to_dict()
        campaign = before.data.get("campaign")
        day = (campaign.get("day") if isinstance(campaign, dict) else
               tick // self.clock.ticks_per_day + 1
               if self.package.story_blueprint is not None and self.clock else None)
        for memory in plan.memories:
            summary = memory.fact.strip()
            if type(day) is int and day > 0:
                summary = f"第{day}天：{summary}"
            observations.append(Observation(
                f"{event_id}:memory:{memory.actor_id}", event_id,
                memory.actor_id, "shared_experience", summary, tick,
            ))
        if decision.intent == "speech":
            hearers = (tuple(actor_id for actor_id, state in actors.items()
                             if actor_id != "player" and isinstance(state, dict)
                             and state.get("location") == location)
                       if decision.channel == "speech" and decision.audience == "room"
                       else tuple(decision.target_ids))
            observations.extend(Observation(f"{event_id}:heard:{actor_id}", event_id,
                                            actor_id, decision.channel, player_text, tick)
                                for actor_id in hearers)
        elif (decision.intent == "action" and isinstance(location, str)
              and details.get("sensory")):
            observations.extend(item for item in physical_observations(before, event)
                                if item.recipient_id != "player")
        pending = []
        counts = {}
        for index, reply in enumerate(plan.replies):
            count = counts.get(reply.actor_id, 0)
            counts[reply.actor_id] = count + 1
            suffix = f':{count}' if count else ''
            pending.append(PendingWork(
                f'{event_id}:reply:{reply.actor_id}{suffix}', 'single_npc_reply', tick,
                10 - index, event_id,
                {'actor_id': reply.actor_id, 'speech': reply.speech,
                 'player_message': player_text if decision.intent == 'speech' else '',
                 'private': decision.channel == 'private_message', 'visible_committed': True}))
        return self.store.commit(before.game_id, before.version, event,
                                 tuple(observations), tuple(pending))

    def proposed_options(self, game_id: str, turn_id: str,
                         authored: tuple[ActionOption, ...] = ()) -> tuple[ActionOption, ...]:
        details = next((self.store.event_details(game_id, f"{turn_id}:{kind}")
                        for kind in ("input", "query", "action")
                        if self.store.event_exists(game_id, f"{turn_id}:{kind}")), None)
        raw = details.get("single_call") if isinstance(details, dict) else None
        if not isinstance(raw, dict):
            return authored
        progress = self.store.load(game_id).data.get("story_progress", {})
        if isinstance(progress, dict) and progress.get("complete") is True:
            return ()
        options = raw.get("options", [])
        previous = self.store.player_inputs_for(game_id)[-4:]
        recent = [item.text for item in previous]
        for item in previous[-2:-1]:
            prior = self.store.event_details(game_id, item.event_id) or {}
            scene = prior.get("single_call")
            if isinstance(scene, dict) and isinstance(scene.get("options"), list):
                for option in scene["options"]:
                    if isinstance(option, dict):
                        recent.extend(str(option.get(key, "")) for key in ("input", "label"))
        return validated_options(options if isinstance(options, list) else [], tuple(recent))
