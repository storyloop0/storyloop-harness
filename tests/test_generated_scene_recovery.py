import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.core.generated_scene import GeneratedSceneRecovery, GeneratedSceneMismatch
from storyloop_harness.core.generated_scene import GeneratedSceneStale, GeneratedSceneWriteFailure
from storyloop_harness.core.contracts import WorldEvent
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import OfflineModel
from storyloop_harness.testing import InMemoryGameStore


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
