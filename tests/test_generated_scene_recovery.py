import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.core.generated_scene import GeneratedSceneRecovery, GeneratedSceneMismatch
from storyloop_harness.core.generated_scene import GeneratedSceneStale, GeneratedSceneWriteFailure
from storyloop_harness.core.contracts import WorldEvent
from storyloop_harness.context import ContextBudget, ContextPolicy
from storyloop_harness.models.attempt_tracking import AttemptTracker
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.runtime.player_preferences import player_preferences_scope
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import OfflineModel
from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.usage import collect_attempts
from storyloop_harness.world.worldbook import Worldbook


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/freeform'


class FailOnceStore(SQLiteGameStore):
    fail = True

    def commit(self, *args, **kwargs):
        if self.fail and args[2] is not None:
            self.fail = False
            raise RuntimeError('injected commit failure')
        return super().commit(*args, **kwargs)


class ForbiddenModel:
    async def __call__(self, *args, **kwargs):
        raise AssertionError('model must not run on recovered scene')


class AttemptedOfflineModel(OfflineModel):
    def __init__(self):
        self.tracker = AttemptTracker('offline', 'scene_turn')

    async def __call__(self, *args, **kwargs):
        with self.tracker.invocation():
            async with self.tracker.attempt():
                return self.tracker.observe(await super().__call__(*args, **kwargs))


def recovery_engine(store, package, model, **kwargs):
    return TurnEngine(store, package, model,
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'), **kwargs)


def assert_one_linked_input(store, request):
    inputs = store.player_inputs_for(request.game_id)
    assert len(inputs) == 1
    assert inputs[0].text == request.player_text
    details = store.event_details(request.game_id, inputs[0].event_id)
    record = store.generated_scene_snapshot(request.game_id, request.turn_id)
    assert details['generated_scene_snapshot_id'] == record.snapshot_id
    assert details['generated_scene_snapshot_checksum'] == record.checksum


def assert_one_npc_delivery(store, request):
    event_id = f'{request.turn_id}:input:reply:dockhand:spoken'
    assert store.event_exists(request.game_id, event_id)
    assert len([entry for entry in store.agent_context_entries(request.game_id, 'dockhand')
                if entry.entry_id == event_id]) == 1
    assert len([item for item in store.observations_for(request.game_id, 'player')
                if item.event_id == f'{request.turn_id}:input' and item.channel == 'dialogue']) == 1
    assert not any(work.work_id == f'{request.turn_id}:input:reply:dockhand'
                   for work in store.pending_work(request.game_id))


def test_default_engine_never_uses_snapshot_port_and_opt_in_requires_identity():
    class LegacyStore(InMemoryGameStore):
        def generated_scene_snapshot(self, *args):
            raise AssertionError('snapshot port touched')
        def save_generated_scene_snapshot(self, *args):
            raise AssertionError('snapshot port touched')
    package = ScenarioPackage.load(EXAMPLE)
    store = LegacyStore()
    package.seed_game(store, 'game')
    result = asyncio.run(TurnEngine(store, package, OfflineModel()).run_turn(
        TurnInput('game', 'Hello', 'turn-1', package.version)))
    assert result.snapshot.version > 0
    with pytest.raises(ValueError, match='identity'):
        GeneratedSceneRecovery(store, '')
    with pytest.raises(ValueError, match='game store'):
        TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(InMemoryGameStore(), 'offline:v1'))


def test_failed_commit_resumes_from_snapshot_without_generation(tmp_path):
    package = ScenarioPackage.load(EXAMPLE)
    path = tmp_path / 'recovery.db'
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    with FailOnceStore(path) as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        with pytest.raises(RuntimeError, match='injected commit'):
            asyncio.run(engine.run_turn(request))
        record = engine.generated_scene_snapshot('game', 'turn-1')
        assert record is not None
        assert record.success_usage()
        assert record.metadata()['usage_coverage'] == 'reported'
        assert store.load('game').version == 0
        assert store.player_inputs_for('game') == []
    with SQLiteGameStore(path) as store:
        engine = TurnEngine(store, package, ForbiddenModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        outcome = asyncio.run(engine.run_turn(request))
        assert outcome.model_usage == ()
        assert store.player_inputs_for('game')[0].text == 'Hello'
        assert engine.generated_scene_snapshot('game', 'turn-1') == record
        assert asyncio.run(engine.run_turn(request)).model_usage == ()


def test_recovery_does_not_spend_an_exhausted_execution_budget(tmp_path):
    """Stored generation can commit and replay after another actual send used the quota."""
    import httpx2
    from model_helpers import create_test_model
    from storyloop_harness.generation import ExecutionBudget, execution_budget_scope

    package = ScenarioPackage.load(EXAMPLE)
    path = tmp_path / 'execution-recovery.db'
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    with FailOnceStore(path) as store:
        package.seed_game(store, 'game')
        with pytest.raises(RuntimeError, match='injected commit'):
            asyncio.run(recovery_engine(store, package, OfflineModel()).run_turn(request))

    async def resume():
        sent = []
        async def transport(request):
            sent.append(request)
            return httpx2.Response(200, json={
                'id': 'offline', 'created': 0, 'model': 'offline-test',
                'object': 'chat.completion', 'choices': [{
                    'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'ok'},
                }],
            })
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client)
        try:
            with SQLiteGameStore(path) as store:
                engine = recovery_engine(store, package, ForbiddenModel())
                async with execution_budget_scope(ExecutionBudget(1, 30)) as control:
                    await model.client.chat.completions.create(
                        model='offline-test', messages=[{'role': 'user', 'content': 'hi'}])
                    with collect_attempts() as attempts:
                        outcome = await engine.run_turn(request)
                        replay = await engine.run_turn(request)
                    assert control.sends == len(sent) == 1
                    assert outcome.model_usage == replay.model_usage == ()
                    assert not attempts.records
                    assert replay.events == ()
                    assert_one_linked_input(store, request)
                    assert_one_npc_delivery(store, request)
                    assert engine.generated_scene_snapshot('game', 'turn-1').success_usage()
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(resume())


def test_changed_input_rejected_before_model(tmp_path):
    package = ScenarioPackage.load(EXAMPLE)
    with FailOnceStore(tmp_path / 'mismatch.db') as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        with pytest.raises(RuntimeError, match='injected commit'):
            asyncio.run(engine.run_turn(TurnInput('game', 'Hello', 'turn-1', package.version)))
        engine = TurnEngine(store, package, ForbiddenModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        with pytest.raises(GeneratedSceneMismatch):
            asyncio.run(engine.run_turn(TurnInput('game', 'Different', 'turn-1', package.version)))


def test_real_child_exit_after_durable_save_then_new_store_resumes(tmp_path):
    path = tmp_path / 'child.db'
    child = subprocess.run([sys.executable, str(Path(__file__).with_name('generated_scene_crash_probe.py')),
                            str(path)], capture_output=True, text=True,
                           env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
                           timeout=30)
    assert child.returncode == 47, child.stderr
    assert 'child_pid=' in child.stdout
    print(child.stdout.strip(), f'exit_code={child.returncode}')
    package = ScenarioPackage.load(EXAMPLE)
    with SQLiteGameStore(path) as store:
        assert store.load('game').version == 0
        assert store.player_inputs_for('game') == []
        record = store.generated_scene_snapshot('game', 'turn-1')
        assert record is not None
        engine = TurnEngine(store, package, ForbiddenModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        outcome = asyncio.run(engine.run_turn(TurnInput('game', 'Hello', 'turn-1', package.version)))
        assert outcome.model_usage == ()
        assert len(store.player_inputs_for('game')) == 1
        assert any(item.kind == 'single_npc_reply' for item in store.pending_work('game')) is False
        assert store.event_exists('game', 'turn-1:input:reply:dockhand:spoken')


def test_changed_identity_clock_and_stale_world_reject_before_model(tmp_path):
    package = ScenarioPackage.load(EXAMPLE)
    with FailOnceStore(tmp_path / 'bindings.db') as store:
        package.seed_game(store, 'game')
        request = TurnInput('game', 'Hello', 'turn-1', package.version)
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        with pytest.raises(RuntimeError, match='injected commit'):
            asyncio.run(engine.run_turn(request))
        for identity, clock in [('offline:v2', None), ('offline:v1', StoryClock(8))]:
            changed = TurnEngine(store, package, ForbiddenModel(), clock=clock,
                generated_scene_recovery=GeneratedSceneRecovery(store, identity))
            with pytest.raises(GeneratedSceneMismatch):
                asyncio.run(changed.run_turn(request))
        store.commit('game', 0, WorldEvent('other', 'test', None, None, 0, ()), (), ())
        with pytest.raises(GeneratedSceneStale):
            asyncio.run(TurnEngine(store, package, ForbiddenModel(),
                generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1')).run_turn(request))


def test_lost_snapshot_acknowledgement_is_resumable(tmp_path):
    class LostAckStore(SQLiteGameStore):
        def save_generated_scene_snapshot(self, snapshot):
            super().save_generated_scene_snapshot(snapshot)
            raise OSError('lost acknowledgement')
    package = ScenarioPackage.load(EXAMPLE)
    path = tmp_path / 'lost.db'
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    with LostAckStore(path) as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        with pytest.raises(GeneratedSceneWriteFailure):
            asyncio.run(engine.run_turn(request))
        assert store.generated_scene_snapshot('game', 'turn-1') is not None
        assert store.load('game').version == 0
    with SQLiteGameStore(path) as store:
        engine = TurnEngine(store, package, ForbiddenModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        assert asyncio.run(engine.run_turn(request)).model_usage == ()


def test_cancel_after_save_preserves_row_without_commit(tmp_path):
    class CancelAfterSave(SQLiteGameStore):
        def save_generated_scene_snapshot(self, snapshot):
            saved = super().save_generated_scene_snapshot(snapshot)
            raise asyncio.CancelledError()
    package = ScenarioPackage.load(EXAMPLE)
    with CancelAfterSave(tmp_path / 'cancel.db') as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(engine.run_turn(TurnInput('game', 'Hello', 'turn-1', package.version)))
        assert store.generated_scene_snapshot('game', 'turn-1') is not None
        assert store.load('game').version == 0


def test_committed_world_write_with_lost_ack_reconciles_linked_event(tmp_path):
    class CommitThenLoseAck(SQLiteGameStore):
        lost = False

        def commit(self, *args, **kwargs):
            committed = super().commit(*args, **kwargs)
            event = args[2]
            if not self.lost and event is not None and 'generated_scene_snapshot_id' in event.details:
                self.lost = True
                raise OSError('world acknowledgement lost')
            return committed

    package = replace(ScenarioPackage.load(EXAMPLE), presentation_mode='interactive')
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    path = tmp_path / 'world-ack.db'
    with CommitThenLoseAck(path) as store:
        package.seed_game(store, 'game')
        with collect_attempts() as fresh_attempts:
            with pytest.raises(OSError, match='acknowledgement'):
                asyncio.run(recovery_engine(store, package, AttemptedOfflineModel()).run_turn(request))
        assert len(fresh_attempts.records) == 1
        assert_one_linked_input(store, request)
        assert any(work.kind == 'single_npc_reply' for work in store.pending_work('game'))
        assert not store.event_exists('game', 'turn-1:input:reply:dockhand:spoken')
    with SQLiteGameStore(path) as store:
        engine = recovery_engine(store, package, ForbiddenModel())
        engine.session.projector.project = lambda *a: (_ for _ in ()).throw(AssertionError('projection reran'))
        with collect_attempts() as restored_attempts:
            outcome = asyncio.run(engine.run_turn(request))
        assert restored_attempts.records == []
        assert outcome.model_usage == ()
        assert outcome.processed_work_ids == ('turn-1:input:reply:dockhand',)
        assert_one_linked_input(store, request)
        assert_one_npc_delivery(store, request)


@pytest.mark.parametrize('interruption', ['before_npc_work', 'after_npc_work'])
def test_linked_event_interruption_resumes_only_remaining_work(tmp_path, interruption):
    package = replace(ScenarioPackage.load(EXAMPLE), presentation_mode='interactive')
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    path = tmp_path / f'{interruption}.db'

    async def interrupt_progress(event):
        if (interruption == 'before_npc_work' and event.get('stage') == 'characters'
                or interruption == 'after_npc_work' and event['type'] == 'segment'):
            raise asyncio.CancelledError()

    with SQLiteGameStore(path) as store:
        package.seed_game(store, 'game')
        with collect_attempts() as fresh_attempts:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(recovery_engine(store, package, AttemptedOfflineModel()).run_turn(
                    request, progress=interrupt_progress))
        assert len(fresh_attempts.records) == 1
        assert_one_linked_input(store, request)
        if interruption == 'before_npc_work':
            assert any(work.kind == 'single_npc_reply' for work in store.pending_work('game'))
            assert not store.event_exists('game', 'turn-1:input:reply:dockhand:spoken')
        else:
            assert_one_npc_delivery(store, request)
    with SQLiteGameStore(path) as store:
        engine = recovery_engine(store, package, ForbiddenModel())
        engine.session.projector.project = lambda *a: (_ for _ in ()).throw(AssertionError('projection reran'))
        with collect_attempts() as restored_attempts:
            outcome = asyncio.run(engine.run_turn(request))
        assert restored_attempts.records == []
        assert outcome.model_usage == ()
        assert outcome.processed_work_ids == (
            ('turn-1:input:reply:dockhand',) if interruption == 'before_npc_work' else ())
        assert_one_linked_input(store, request)
        assert_one_npc_delivery(store, request)


def test_fresh_usage_and_attempts_are_not_restored_on_replay(tmp_path):
    package = ScenarioPackage.load(EXAMPLE)
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    with SQLiteGameStore(tmp_path / 'usage.db') as store:
        package.seed_game(store, 'game')
        with collect_attempts() as fresh_attempts:
            fresh = asyncio.run(recovery_engine(store, package, AttemptedOfflineModel()).run_turn(request))
        record = store.generated_scene_snapshot('game', 'turn-1')
        assert fresh.model_usage == record.success_usage()
        assert len(fresh_attempts.records) == 1
        assert fresh_attempts.records[0].usage == fresh.model_usage[0]
        with collect_attempts() as restored_attempts:
            replay = asyncio.run(recovery_engine(store, package, ForbiddenModel()).run_turn(request))
        assert restored_attempts.records == []
        assert replay.model_usage == ()
        assert record.success_usage() == fresh.model_usage


def test_same_version_package_context_and_policy_changes_reject_before_projection(tmp_path):
    package = ScenarioPackage.load(EXAMPLE)
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    with FailOnceStore(tmp_path / 'config.db') as store:
        package.seed_game(store, 'game')
        with pytest.raises(RuntimeError, match='injected commit'):
            asyncio.run(recovery_engine(store, package, OfflineModel()).run_turn(request))
        original = package.worldbook.entries[0]
        changed_book = Worldbook(package.package_id, package.version, [
            replace(original, text=original.text + ' Revised.'), *package.worldbook.entries[1:]])
        variants = [
            (replace(package, worldbook=changed_book), {}),
            (package, {'context_budget': ContextBudget(context_window_tokens=32768)}),
            (package, {'context_policy': ContextPolicy.RECENT}),
        ]
        for changed_package, options in variants:
            engine = recovery_engine(store, changed_package, ForbiddenModel(), **options)
            engine.session.projector.project = lambda *a: (_ for _ in ()).throw(AssertionError('projection reran'))
            with pytest.raises(GeneratedSceneMismatch):
                asyncio.run(engine.run_turn(request))
            assert store.load('game').version == 0
            assert store.player_inputs_for('game') == []


def test_saved_scene_rejects_changed_system_prompt_binding(tmp_path, monkeypatch):
    import storyloop_harness.runtime.single_call as single_call

    package = ScenarioPackage.load(EXAMPLE)
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    with FailOnceStore(tmp_path / 'prompt-binding.db') as store:
        package.seed_game(store, 'game')
        with pytest.raises(RuntimeError, match='injected commit'):
            asyncio.run(recovery_engine(store, package, OfflineModel()).run_turn(request))
        record = store.generated_scene_snapshot('game', 'turn-1')
        assert record is not None
        original = single_call.scene_system_prompt
        monkeypatch.setattr(single_call, 'scene_system_prompt',
                            lambda current: original(current) + ' changed prompt')
        engine = recovery_engine(store, package, ForbiddenModel())
        engine.session.projector.project = lambda *args: (_ for _ in ()).throw(
            AssertionError('projection reran'))
        with pytest.raises(GeneratedSceneMismatch):
            asyncio.run(engine.run_turn(request))
        assert store.generated_scene_snapshot('game', 'turn-1') == record
        assert store.player_inputs_for('game') == []


def test_changed_preferences_alone_resume_saved_scene_without_projection(tmp_path):
    package = ScenarioPackage.load(EXAMPLE)
    request = TurnInput('game', 'Hello', 'turn-1', package.version)
    with FailOnceStore(tmp_path / 'preferences.db') as store:
        package.seed_game(store, 'game')
        with player_preferences_scope(('short replies',)):
            with pytest.raises(RuntimeError, match='injected commit'):
                asyncio.run(recovery_engine(store, package, OfflineModel()).run_turn(request))
        record = store.generated_scene_snapshot('game', 'turn-1')
        engine = recovery_engine(store, package, ForbiddenModel())
        engine.session.projector.project = lambda *a: (_ for _ in ()).throw(AssertionError('projection reran'))
        with player_preferences_scope(('detailed replies',)):
            with collect_attempts() as restored_attempts:
                outcome = asyncio.run(engine.run_turn(request))
        assert restored_attempts.records == []
        assert outcome.model_usage == ()
        assert store.event_details('game', 'turn-1:input')['single_call'] == record.scene().model_dump()
        assert_one_linked_input(store, request)
