"""Default single-call engine facade."""
from contextvars import ContextVar
import re
import logging
from storyloop_harness.context import ContextBudget, ContextProjection, ContextPolicy
from dataclasses import replace
from storyloop_harness.contracts import TurnInput, TurnOutcome
from storyloop_harness.ports import GameStore, ModelPort, CampaignContext
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.runtime.turn_progress import TurnProgress
from storyloop_harness.runtime.runner import RunResult
from storyloop_harness.adapters.telemetry import Telemetry
from storyloop_harness.agents.action_advisor import ActionOption
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.agents.scene_turn import SingleSceneGenerator
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.context.checkpoints import CheckpointLifecycle, CheckpointSummarizer
from storyloop_harness.runtime.single_call import SingleCallGameSession
from storyloop_harness.models.usage import collect_usage
from storyloop_harness.models.execution_budget import ExecutionRequestLimitExceeded, ExecutionTimeout
from storyloop_harness.core.generated_scene import (
    GeneratedSceneRecovery, GeneratedSceneRecoveryInspection, text_hash,
)

logger = logging.getLogger(__name__)


class _RecordingStore:
    """Collect only successful commits in the current asynchronous turn."""
    def __init__(self, store: GameStore):
        self.store = store
        self.records = ContextVar('turn_commits', default=None)

    def __getattr__(self, name):
        return getattr(self.store, name)

    def commit(self, game_id, expected_version, event, observations, new_work, consumed_work_id=None):
        result = self.store.commit(game_id, expected_version, event, observations, new_work, consumed_work_id)
        records = self.records.get()
        if records is not None and event is not None:
            records.append(event)
        return result


class TurnEngine:
    def __init__(self, store: GameStore, package: ScenarioPackage, model: ModelPort,
                 *, clock: StoryClock | None = None, program: CampaignContext | None = None,
                 max_responders: int = 3, context_budget: ContextBudget | None = None,
                 max_steps: int = 8, telemetry: Telemetry | None = None,
                 checkpoint_summarizer: CheckpointSummarizer | None = None,
                 context_policy: ContextPolicy | None = None,
                 generated_scene_recovery: GeneratedSceneRecovery | None = None):
        if generated_scene_recovery is not None:
            if generated_scene_recovery.snapshot_store is not store:
                raise ValueError('generated scene store must be the engine game store')
            if not all(callable(getattr(store, name, None)) for name in (
                    'generated_scene_snapshot', 'save_generated_scene_snapshot')):
                raise ValueError('generated scene store does not implement snapshot port')
        self.package = package
        self.generated_scene_recovery = generated_scene_recovery
        self.store = _RecordingStore(store)
        self.checkpoints = (CheckpointLifecycle(self.store, package, checkpoint_summarizer)
                            if checkpoint_summarizer is not None else None)
        self.session = SingleCallGameSession(
            self.store, package, SingleSceneGenerator(model, package, telemetry),
            SceneContextProjector(self.store, package, clock=clock, program=program,
                                  max_responders=max_responders,
                                  context_budget=context_budget, context_policy=context_policy),
            clock=clock, max_steps=max_steps, telemetry=telemetry,
            generated_scene_recovery=generated_scene_recovery)

    def generated_scene_snapshot(self, game_id: str, turn_id: str):
        if self.generated_scene_recovery is None:
            raise ValueError('generated scene recovery is not configured')
        return self.generated_scene_recovery.snapshot_store.generated_scene_snapshot(game_id, turn_id)

    def inspect_generated_scene_recovery(
            self, turn: TurnInput, *, max_tick: int | None = None,
            ) -> GeneratedSceneRecoveryInspection | None:
        return self.inspect_generated_scene_recovery_fingerprint(
            turn.game_id, turn.turn_id, text_hash(turn.player_text), turn.scenario_version,
            max_tick=max_tick)

    def inspect_generated_scene_recovery_fingerprint(
            self, game_id: str, turn_id: str, input_hash: str, scenario_version: str,
            *, max_tick: int | None = None) -> GeneratedSceneRecoveryInspection | None:
        if self.generated_scene_recovery is None:
            raise ValueError('generated scene recovery is not configured')
        if not isinstance(input_hash, str) or re.fullmatch(r'[0-9a-f]{64}', input_hash) is None:
            raise ValueError('invalid input fingerprint')
        before = self._snapshot_for(TurnInput(game_id, '', turn_id, scenario_version))
        return self.session.inspect_generated_scene_recovery(
            before, turn_id, input_hash, max_tick)

    def _snapshot_for(self, turn: TurnInput):
        if turn.scenario_version != self.package.version:
            raise ValueError('scenario version does not match engine package')
        snapshot = self.store.load(turn.game_id)
        scenario = snapshot.data.get('scenario', {})
        if scenario.get('id') != self.package.package_id or scenario.get('version') != self.package.version:
            raise ValueError('saved scenario identity/version does not match engine package')
        return snapshot

    def project_context(self, turn: TurnInput) -> ContextProjection:
        """Inspect a detached ordinary-scene request without generation or writes."""
        snapshot = self._snapshot_for(turn)
        return self.session.projector.project(snapshot, turn.player_text)

    async def run_turn(self, turn: TurnInput, *, progress: TurnProgress | None = None,
                       max_tick: int | None = None) -> TurnOutcome:
        self._snapshot_for(turn)
        events = []
        token = self.store.records.set(events)
        try:
            with collect_usage() as usage:
                result = await self.session.run_turn(turn.game_id, turn.player_text, turn.turn_id,
                                                     progress=progress, max_tick=max_tick)
                if self.checkpoints is not None and events:
                    for actor_id in ('player', *(actor for actor, _ in self.package.actor_cards)):
                        try:
                            await self.checkpoints.refresh(turn.game_id, actor_id)
                        except (ExecutionRequestLimitExceeded, ExecutionTimeout):
                            raise
                        except Exception as exc:
                            logger.warning('checkpoint refresh failed (%s)', type(exc).__name__)
            return replace(result, events=tuple(events), model_usage=tuple(usage.records))
        finally:
            self.store.records.reset(token)

    async def run_ready_work(self, game_id: str,
                             progress: TurnProgress | None = None) -> RunResult:
        """Drain supported persisted work, including already generated NPC speech."""
        return await self.session.run_ready_work(game_id, progress)

    def proposed_options(self, game_id: str, turn_id: str,
                         authored: tuple[ActionOption, ...] = ()) -> tuple[ActionOption, ...]:
        """Read validated action suggestions persisted by a completed turn."""
        return self.session.proposed_options(game_id, turn_id, authored)
