"""Lifecycle tests for the optional uncertain generation guard."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.advanced import (
    GeneratedSceneRecovery, GeneratedSceneGenerationMarker, GeneratedSceneUncertain,
    GeneratedSceneWriteFailure, GeneratedSceneCorrupt, GeneratedSceneMismatch,
    GeneratedSceneStale,
)
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded, ContextPolicy
from storyloop_harness.core.contracts import WorldEvent
from storyloop_harness.core.generated_scene import text_hash
from storyloop_harness.generation import ExecutionTimeout
from storyloop_harness.models.usage import collect_usage
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.testing import InMemoryGameStore, OfflineModel
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.usage import collect_attempts
from storyloop_harness.world.worldbook import Worldbook


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/freeform'


class CountedOfflineModel(OfflineModel):
    def __init__(self):
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return await super().__call__(*args, **kwargs)


def setup(store=None, protect=True, model=None, **options):
    package = ScenarioPackage.load(EXAMPLE)
    store = store if store is not None else InMemoryGameStore()
    package.seed_game(store, 'game')
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    model = model if model is not None else CountedOfflineModel()
    engine = TurnEngine(store, package, model, generated_scene_recovery=
        GeneratedSceneRecovery(store, 'offline:v1', protect_uncertain_generation=protect), **options)
    return package, store, turn, engine, model


def forbidden(*args, **kwargs):
    raise AssertionError('uncertain retry performed work')


def assert_blocked(engine, store, turn, monkeypatch):
    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    monkeypatch.setattr(store, 'commit', forbidden)
    monkeypatch.setattr(store, 'create_generated_scene_generation_marker', forbidden)
    with collect_usage() as usage, collect_attempts() as attempts:
        with pytest.raises(GeneratedSceneUncertain, match='^Generated scene outcome is uncertain$'):
            engine.inspect_generated_scene_recovery(turn)
        with pytest.raises(GeneratedSceneUncertain):
            engine.inspect_generated_scene_recovery_fingerprint(
                turn.game_id, turn.turn_id, text_hash(turn.player_text), turn.scenario_version)
        with pytest.raises(GeneratedSceneUncertain):
            asyncio.run(engine.run_turn(turn))
    assert usage.records == []
    assert attempts.records == []


@pytest.mark.parametrize('error', [RuntimeError('primary failure'), OSError('network'),
    asyncio.CancelledError('cancelled'), ExecutionTimeout()])
def test_post_marker_generator_error_is_preserved_and_retry_is_blocked(error, monkeypatch):
    _, store, turn, engine, model = setup()
    before = store.load('game')

    async def fail(*args, **kwargs):
        assert store.generated_scene_generation_marker('game', 'turn-1') is not None
        raise error

    monkeypatch.setattr(engine.session.generator, 'generate', fail)
    with pytest.raises(type(error)) as raised:
        asyncio.run(engine.run_turn(turn))
    assert raised.value is error
    marker = store.generated_scene_generation_marker('game', 'turn-1')
    assert marker is not None and model.calls == 0
    assert store.load('game') == before
    assert store.generated_scene_snapshot('game', 'turn-1') is None
    assert store.player_inputs_for('game') == []
    assert_blocked(engine, store, turn, monkeypatch)
    assert store.generated_scene_generation_marker('game', 'turn-1') == marker


@pytest.mark.parametrize('persist_first', [False, True])
def test_marker_write_failure_never_enters_generator(persist_first, monkeypatch):
    _, store, turn, engine, model = setup()
    original = store.create_generated_scene_generation_marker
    failure = OSError('private store failure')

    def fail(marker):
        if persist_first:
            original(marker)
        raise failure

    monkeypatch.setattr(store, 'create_generated_scene_generation_marker', fail)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    with pytest.raises(GeneratedSceneWriteFailure) as caught:
        asyncio.run(engine.run_turn(turn))
    assert caught.value.__cause__ is failure
    assert 'private store failure' not in str(caught.value)
    assert (store.generated_scene_generation_marker('game', 'turn-1') is not None) is persist_first
    assert store.generated_scene_snapshot('game', 'turn-1') is None
    assert store.load('game').version == 0 and store.player_inputs_for('game') == []
    assert model.calls == 0
    if persist_first:
        assert_blocked(engine, store, turn, monkeypatch)
    else:
        assert engine.inspect_generated_scene_recovery(turn) is None
        monkeypatch.setattr(store, 'create_generated_scene_generation_marker', original)
        fresh = TurnEngine(store, engine.package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        asyncio.run(fresh.run_turn(turn))
        assert model.calls == 1


def test_normal_guarded_turn_replays_without_model_or_usage():
    _, store, turn, engine, model = setup()
    first = asyncio.run(engine.run_turn(turn))
    marker = store.generated_scene_generation_marker('game', 'turn-1')
    assert marker is not None and model.calls == 1
    assert store.generated_scene_snapshot('game', 'turn-1') is not None
    assert first.model_usage
    with collect_usage() as usage:
        replay = asyncio.run(engine.run_turn(turn))
    assert model.calls == 1 and replay.model_usage == () and usage.records == []
    assert len(store.player_inputs_for('game')) == 1
    assert store.event_exists('game', 'turn-1:input:reply:dockhand:spoken')
    assert store.generated_scene_generation_marker('game', 'turn-1') == marker


def test_enabled_constructor_requires_marker_port_and_disabled_does_not_touch_it():
    class LegacyStore:
        def __init__(self):
            self.inner = InMemoryGameStore()

        def __getattr__(self, name):
            if name in ('generated_scene_generation_marker',
                        'create_generated_scene_generation_marker'):
                raise AttributeError(name)
            return getattr(self.inner, name)

    class TrapStore(InMemoryGameStore):
        @property
        def generated_scene_generation_marker(self):
            raise AssertionError('read marker attribute')

        @property
        def create_generated_scene_generation_marker(self):
            raise AssertionError('claim marker attribute')

    package = ScenarioPackage.load(EXAMPLE)
    old = LegacyStore()
    package.seed_game(old, 'game')
    with pytest.raises(ValueError, match='generation marker port'):
        TurnEngine(old, package, CountedOfflineModel(), generated_scene_recovery=
            GeneratedSceneRecovery(old, 'offline:v1', True))
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    for store in (old, TrapStore()):
        if store is not old:
            package.seed_game(store, 'game')
        engine = TurnEngine(store, package, CountedOfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        assert engine.inspect_generated_scene_recovery(turn) is None
        asyncio.run(engine.run_turn(turn))
        assert engine.inspect_generated_scene_recovery(turn).committed is True
        assert asyncio.run(engine.run_turn(turn)).model_usage == ()
    fresh = TrapStore()
    package.seed_game(fresh, 'game')
    assert asyncio.run(TurnEngine(fresh, package, CountedOfflineModel()).run_turn(turn)).snapshot.version > 0


def test_guarded_empty_inspection_is_read_only(monkeypatch):
    _, store, turn, engine, _ = setup()
    before = store.load('game')
    pending = store.pending_work('game')
    for obj, name in ((engine.session.projector, 'project'),
                      (engine.session.generator, 'generate'),
                      (store, 'commit'), (store, 'save_generated_scene_snapshot'),
                      (store, 'create_generated_scene_generation_marker')):
        monkeypatch.setattr(obj, name, forbidden)
    with collect_usage() as usage, collect_attempts() as attempts:
        assert engine.inspect_generated_scene_recovery(turn) is None
        assert engine.inspect_generated_scene_recovery_fingerprint(
            turn.game_id, turn.turn_id, text_hash(turn.player_text), turn.scenario_version) is None
    assert usage.records == [] and attempts.records == []
    assert store.load('game') == before and store.pending_work('game') == pending
    assert store.player_inputs_for('game') == []


def test_projection_budget_rejection_precedes_marker_and_allows_same_turn_retry():
    package, store, turn, _, model = setup()
    tiny = TurnEngine(store, package, model,
        context_budget=ContextBudget(context_window_tokens=2, output_tokens=1,
                                     safety_margin_tokens=0),
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
    before = store.load('game')
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(tiny.run_turn(turn))
    assert store.generated_scene_generation_marker('game', 'turn-1') is None
    assert store.generated_scene_snapshot('game', 'turn-1') is None
    assert store.load('game') == before and model.calls == 0
    normal = TurnEngine(store, package, model,
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
    asyncio.run(normal.run_turn(turn))
    assert model.calls == 1


def test_other_projection_failure_precedes_marker_and_preserves_error(monkeypatch):
    package, store, turn, engine, model = setup()
    error = RuntimeError('projector failed')
    original = engine.session.projector.project

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(engine.session.projector, 'project', fail)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(engine.run_turn(turn))
    assert caught.value is error
    assert store.generated_scene_generation_marker('game', 'turn-1') is None
    assert model.calls == 0
    monkeypatch.setattr(engine.session.projector, 'project', original)
    asyncio.run(engine.run_turn(turn))
    assert model.calls == 1


@pytest.mark.parametrize('mismatch', [False, True])
def test_atomic_claim_loser_checks_winning_marker(mismatch, monkeypatch):
    _, store, turn, engine, model = setup()
    original = store.create_generated_scene_generation_marker

    def race(marker):
        if mismatch:
            bindings = {key: value for key, value in marker.metadata().items()
                        if key not in ('format_version', 'generation_contract_version', 'marker_id')}
            marker = GeneratedSceneGenerationMarker.create(game_id='game', turn_id='turn-1',
                **{key: value for key, value in bindings.items()
                   if key not in ('game_id', 'turn_id', 'input_hash')}, input_hash='9' * 64)
        assert original(marker) is True
        return False

    monkeypatch.setattr(store, 'create_generated_scene_generation_marker', race)
    expected = GeneratedSceneMismatch if mismatch else GeneratedSceneUncertain
    with pytest.raises(expected):
        asyncio.run(engine.run_turn(turn))
    assert model.calls == 0
    assert store.generated_scene_generation_marker('game', 'turn-1') is not None
    assert store.generated_scene_snapshot('game', 'turn-1') is None
    assert store.player_inputs_for('game') == []


@pytest.mark.parametrize('ack,error_type', [
    (None, GeneratedSceneWriteFailure), (1, GeneratedSceneWriteFailure),
    ('yes', GeneratedSceneWriteFailure), (False, GeneratedSceneCorrupt)])
def test_broken_claim_acknowledgement_never_generates(ack, error_type, monkeypatch):
    _, store, turn, engine, model = setup()
    monkeypatch.setattr(store, 'create_generated_scene_generation_marker', lambda marker: ack)
    with pytest.raises(error_type):
        asyncio.run(engine.run_turn(turn))
    assert model.calls == 0
    assert store.generated_scene_generation_marker('game', 'turn-1') is None
    assert engine.inspect_generated_scene_recovery(turn) is None


def test_claim_cancellation_retains_marker_and_original_identity(monkeypatch):
    _, store, turn, engine, model = setup()
    original = store.create_generated_scene_generation_marker
    error = asyncio.CancelledError('cancel after persist')

    def cancel(marker):
        original(marker)
        raise error

    monkeypatch.setattr(store, 'create_generated_scene_generation_marker', cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(engine.run_turn(turn))
    assert caught.value is error and model.calls == 0
    assert store.generated_scene_generation_marker('game', 'turn-1') is not None
    with pytest.raises(GeneratedSceneUncertain):
        engine.inspect_generated_scene_recovery(turn)


def test_validation_failure_after_model_result_retains_marker(monkeypatch):
    _, store, turn, engine, model = setup()
    error = ValueError('invalid scene')

    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(engine.session, '_validate_plan', reject)
    with collect_usage() as usage:
        with pytest.raises(ValueError) as caught:
            asyncio.run(engine.run_turn(turn))
    assert caught.value is error and model.calls == 1 and usage.records == []
    assert store.generated_scene_generation_marker('game', 'turn-1') is not None
    assert store.generated_scene_snapshot('game', 'turn-1') is None
    assert store.player_inputs_for('game') == []
    with collect_usage() as retry_usage:
        with pytest.raises(GeneratedSceneUncertain):
            asyncio.run(engine.run_turn(turn))
    assert model.calls == 1 and retry_usage.records == []


@pytest.mark.parametrize('persist_first', [False, True])
def test_snapshot_failure_keeps_marker_and_saved_result_priority(persist_first, monkeypatch):
    package, store, turn, engine, model = setup()
    original = store.save_generated_scene_snapshot
    failure = OSError('snapshot acknowledgement lost')

    def fail(snapshot):
        if persist_first:
            original(snapshot)
        raise failure

    monkeypatch.setattr(store, 'save_generated_scene_snapshot', fail)
    with pytest.raises(GeneratedSceneWriteFailure) as caught:
        asyncio.run(engine.run_turn(turn))
    assert caught.value.__cause__ is failure
    assert model.calls == 1
    assert store.generated_scene_generation_marker('game', 'turn-1') is not None
    assert (store.generated_scene_snapshot('game', 'turn-1') is not None) is persist_first
    assert store.player_inputs_for('game') == []
    if persist_first:
        inspection = engine.inspect_generated_scene_recovery(turn)
        assert inspection is not None and not inspection.committed
        monkeypatch.setattr(engine.session.projector, 'project', forbidden)
        monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
        with collect_usage() as usage:
            result = asyncio.run(engine.run_turn(turn))
        assert result.model_usage == () and usage.records == []
        assert len(store.player_inputs_for('game')) == 1
    else:
        with pytest.raises(GeneratedSceneUncertain):
            engine.inspect_generated_scene_recovery(turn)


def test_snapshot_cancel_after_persist_resumes_without_second_model(monkeypatch):
    _, store, turn, engine, model = setup()
    original = store.save_generated_scene_snapshot
    error = asyncio.CancelledError('after save')

    def cancel(snapshot):
        original(snapshot)
        raise error

    monkeypatch.setattr(store, 'save_generated_scene_snapshot', cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(engine.run_turn(turn))
    assert caught.value is error and model.calls == 1
    assert store.generated_scene_generation_marker('game', 'turn-1') is not None
    assert store.generated_scene_snapshot('game', 'turn-1') is not None
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    assert asyncio.run(engine.run_turn(turn)).model_usage == ()
    assert model.calls == 1


def test_marker_write_ack_lost_is_durable_across_connection(tmp_path):
    class LostMarkerAck(SQLiteGameStore):
        def create_generated_scene_generation_marker(self, marker):
            super().create_generated_scene_generation_marker(marker)
            raise OSError('ack lost')

    package = ScenarioPackage.load(EXAMPLE)
    path = tmp_path / 'marker-ack.db'
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    model = CountedOfflineModel()
    with LostMarkerAck(path) as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        with pytest.raises(GeneratedSceneWriteFailure):
            asyncio.run(engine.run_turn(turn))
        assert model.calls == 0
    with SQLiteGameStore(path) as store:
        engine = TurnEngine(store, package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        assert store.generated_scene_generation_marker('game', 'turn-1') is not None
        assert store.generated_scene_snapshot('game', 'turn-1') is None
        with pytest.raises(GeneratedSceneUncertain):
            engine.inspect_generated_scene_recovery(turn)
        with pytest.raises(GeneratedSceneUncertain):
            asyncio.run(engine.run_turn(turn))
        assert model.calls == 0


def test_snapshot_ack_lost_reopens_and_recovers_without_usage(tmp_path, monkeypatch):
    class LostSnapshotAck(SQLiteGameStore):
        def save_generated_scene_snapshot(self, snapshot):
            super().save_generated_scene_snapshot(snapshot)
            raise OSError('ack lost')

    package = ScenarioPackage.load(EXAMPLE)
    path = tmp_path / 'snapshot-ack.db'
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    model = CountedOfflineModel()
    with LostSnapshotAck(path) as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        with pytest.raises(GeneratedSceneWriteFailure):
            asyncio.run(engine.run_turn(turn))
        assert model.calls == 1
    with SQLiteGameStore(path) as store:
        engine = TurnEngine(store, package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        assert store.generated_scene_generation_marker('game', 'turn-1') is not None
        assert engine.inspect_generated_scene_recovery(turn).snapshot.success_usage()
        monkeypatch.setattr(engine.session.projector, 'project', forbidden)
        monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
        with collect_usage() as usage:
            result = asyncio.run(engine.run_turn(turn))
        assert result.model_usage == () and usage.records == [] and model.calls == 1
        assert len(store.player_inputs_for('game')) == 1


def make_uncertain(monkeypatch):
    package, store, turn, engine, model = setup()
    error = RuntimeError('generation failed')

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(engine.session.generator, 'generate', fail)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(engine.run_turn(turn))
    assert caught.value is error
    assert model.calls == 0
    marker = store.generated_scene_generation_marker('game', 'turn-1')
    assert marker is not None
    return package, store, turn, engine, model, marker


@pytest.mark.parametrize('change', [
    'text', 'identity', 'clock', 'max_tick', 'worldbook',
    'context_policy', 'context_budget', 'max_responders'])
def test_marker_binding_changes_reject_before_projection(change, monkeypatch):
    package, store, turn, _, model, marker = make_uncertain(monkeypatch)
    options = {}
    identity = 'offline:v1'
    if change == 'text':
        turn = replace(turn, player_text='Different')
    elif change == 'identity':
        identity = 'offline:v2'
    elif change == 'clock':
        options['clock'] = StoryClock(8)
    elif change == 'max_tick':
        options['max_tick'] = 4
    elif change == 'worldbook':
        original = package.worldbook.entries[0]
        book = Worldbook(package.package_id, package.version, [
            replace(original, text=original.text + ' Revised.'), *package.worldbook.entries[1:]])
        package = replace(package, worldbook=book)
    elif change == 'context_policy':
        options['context_policy'] = ContextPolicy.RECENT
    elif change == 'context_budget':
        options['context_budget'] = ContextBudget(context_window_tokens=32768)
    elif change == 'max_responders':
        options['max_responders'] = 1
    max_tick = options.pop('max_tick', None)
    changed = TurnEngine(store, package, model,
        generated_scene_recovery=GeneratedSceneRecovery(store, identity, True), **options)
    monkeypatch.setattr(changed.session.projector, 'project', forbidden)
    with collect_usage() as usage:
        with pytest.raises(GeneratedSceneMismatch):
            changed.inspect_generated_scene_recovery(turn, max_tick=max_tick)
        with pytest.raises(GeneratedSceneMismatch):
            asyncio.run(changed.run_turn(turn, max_tick=max_tick))
    assert usage.records == [] and model.calls == 0
    assert store.generated_scene_generation_marker('game', 'turn-1') == marker
    assert store.generated_scene_snapshot('game', 'turn-1') is None
    assert store.player_inputs_for('game') == []


@pytest.mark.parametrize('change', ['version', 'tick', 'state'])
def test_marker_world_base_change_is_stale(change, monkeypatch):
    _, store, turn, engine, model, marker = make_uncertain(monkeypatch)
    before = store.load('game')
    if change == 'version':
        store.commit('game', before.version,
            WorldEvent('other', 'test', None, None, before.tick, ()), (), ())
    elif change == 'tick':
        store._games['game']['snapshot'] = replace(before, tick=before.tick + 1)
    else:
        data = {**before.data, 'world': {**before.data['world'], 'location': 'elsewhere'}}
        store._games['game']['snapshot'] = replace(before, data=data)
    with pytest.raises(GeneratedSceneStale):
        engine.inspect_generated_scene_recovery(turn)
    with pytest.raises(GeneratedSceneStale):
        asyncio.run(engine.run_turn(turn))
    assert model.calls == 0
    assert store.generated_scene_generation_marker('game', 'turn-1') == marker


@pytest.mark.parametrize('corruption', ['game_id', 'turn_id', 'checksum', 'marker_id', 'type'])
def test_corrupt_marker_wrapper_rejected_by_inspection_and_execution(corruption, monkeypatch):
    _, store, turn, engine, model, marker = make_uncertain(monkeypatch)
    changes = {'game_id': 'other', 'turn_id': 'other',
               'checksum': '0' * 64, 'marker_id': '0' * 64}
    corrupt = object() if corruption == 'type' else replace(
        marker, **{corruption: changes[corruption]})
    monkeypatch.setattr(store, 'generated_scene_generation_marker', lambda *args: corrupt)
    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    with pytest.raises(GeneratedSceneCorrupt):
        engine.inspect_generated_scene_recovery(turn)
    with pytest.raises(GeneratedSceneCorrupt):
        asyncio.run(engine.run_turn(turn))
    assert model.calls == 0


def test_snapshot_corruption_has_priority_over_valid_marker(monkeypatch):
    package, store, turn, engine, model = setup()
    class FailCommit(Exception):
        pass
    error = FailCommit()
    original = store.commit

    def fail(*args, **kwargs):
        if args[2] is not None:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'commit', fail)
    with pytest.raises(FailCommit):
        asyncio.run(engine.run_turn(turn))
    assert model.calls == 1
    marker = store.generated_scene_generation_marker('game', 'turn-1')
    assert marker is not None
    row = store._games['game']['snapshots']['turn-1']
    store._games['game']['snapshots']['turn-1'] = (row[0], '0' * 64)
    with pytest.raises(GeneratedSceneCorrupt):
        engine.inspect_generated_scene_recovery(turn)
    with pytest.raises(GeneratedSceneCorrupt):
        asyncio.run(engine.run_turn(turn))
    assert model.calls == 1


@pytest.mark.parametrize('insert_marker', [False, True])
def test_valid_legacy_snapshot_precedes_corrupt_marker_lookup(insert_marker, monkeypatch):
    package, store, turn, _, model = setup(protect=False)
    error = RuntimeError('commit failed')
    original = store.commit

    def fail(*args, **kwargs):
        if args[2] is not None:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'commit', fail)
    with pytest.raises(RuntimeError):
        asyncio.run(TurnEngine(store, package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1')).run_turn(turn))
    snapshot = store.generated_scene_snapshot('game', 'turn-1')
    assert snapshot is not None and snapshot.success_usage()
    assert store.generated_scene_generation_marker('game', 'turn-1') is None
    engine = TurnEngine(store, package, model,
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
    if insert_marker:
        marker = GeneratedSceneGenerationMarker.create(game_id='game', turn_id='turn-1',
            **engine.session._bindings(store.load('game'), text_hash(turn.player_text), None))
        assert store.create_generated_scene_generation_marker(marker) is True
    monkeypatch.setattr(store, 'generated_scene_generation_marker', forbidden)
    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    assert engine.inspect_generated_scene_recovery(turn).snapshot == snapshot
    monkeypatch.setattr(store, 'commit', original)
    assert asyncio.run(engine.run_turn(turn)).model_usage == ()
    assert model.calls == 1
    assert store.generated_scene_snapshot('game', 'turn-1') == snapshot


def test_corrupt_committed_reference_precedes_marker(monkeypatch):
    _, store, turn, engine, model = setup()
    asyncio.run(engine.run_turn(turn))
    assert model.calls == 1
    version, event = store._games['game']['events']['turn-1:input']
    store._games['game']['events']['turn-1:input'] = (version, replace(event,
        details={**event.details, 'generated_scene_snapshot_checksum': '0' * 64}))
    monkeypatch.setattr(store, 'generated_scene_generation_marker', forbidden)
    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    with pytest.raises(GeneratedSceneCorrupt):
        engine.inspect_generated_scene_recovery(turn)
    with pytest.raises(GeneratedSceneCorrupt):
        asyncio.run(engine.run_turn(turn))


def test_valid_legacy_committed_event_ignores_unrelated_marker(monkeypatch):
    package = ScenarioPackage.load(EXAMPLE)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    old = asyncio.run(TurnEngine(store, package, OfflineModel()).run_turn(turn))
    engine = TurnEngine(store, package, OfflineModel(),
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
    marker = GeneratedSceneGenerationMarker.create(game_id='game', turn_id='turn-1',
        **engine.session._bindings(store.load('game'), text_hash('Different'), None))
    assert store.create_generated_scene_generation_marker(marker) is True
    monkeypatch.setattr(store, 'generated_scene_generation_marker', forbidden)
    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    assert engine.inspect_generated_scene_recovery(turn) is None
    with collect_usage() as usage, collect_attempts() as attempts:
        replay = asyncio.run(engine.run_turn(turn))
    assert replay.snapshot == old.snapshot
    assert replay.model_usage == () and replay.events == ()
    assert usage.records == [] and attempts.records == []
    assert len(store.player_inputs_for('game')) == 1


@pytest.mark.parametrize('invalid', ['text', 'scene'])
def test_invalid_legacy_committed_event_rejects_before_marker(invalid, monkeypatch):
    package = ScenarioPackage.load(EXAMPLE)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    asyncio.run(TurnEngine(store, package, OfflineModel()).run_turn(turn))
    engine = TurnEngine(store, package, OfflineModel(),
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
    marker = GeneratedSceneGenerationMarker.create(game_id='game', turn_id='turn-1',
        **engine.session._bindings(store.load('game'), text_hash('Different'), None))
    store.create_generated_scene_generation_marker(marker)
    version, event = store._games['game']['events']['turn-1:input']
    details = dict(event.details)
    if invalid == 'text':
        details['text'] = 'Wrong'
    else:
        details['single_call'] = {'not': 'a scene'}
    store._games['game']['events']['turn-1:input'] = (version, replace(event, details=details))
    monkeypatch.setattr(store, 'generated_scene_generation_marker', forbidden)
    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    with pytest.raises(ValueError):
        engine.inspect_generated_scene_recovery(turn)
    with pytest.raises(ValueError):
        asyncio.run(engine.run_turn(turn))


def test_different_turn_can_generate_after_uncertain_marker(monkeypatch):
    package, store, turn, _, _, marker = make_uncertain(monkeypatch)
    model = CountedOfflineModel()
    engine = TurnEngine(store, package, model,
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
    second = replace(turn, turn_id='turn-2')
    result = asyncio.run(engine.run_turn(second))
    assert result.model_usage and model.calls == 1
    assert store.generated_scene_generation_marker('game', 'turn-1') == marker
    assert store.generated_scene_generation_marker('game', 'turn-2') is not None
    assert store.generated_scene_snapshot('game', 'turn-1') is None
    assert store.generated_scene_snapshot('game', 'turn-2') is not None


def test_guarded_commit_failure_recovers_without_duplicate_delivery(monkeypatch):
    package, store, turn, engine, model = setup()
    error = RuntimeError('commit failed')
    original = store.commit
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed and args[2] is not None:
            failed = True
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'commit', fail_once)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(engine.run_turn(turn))
    assert caught.value is error and model.calls == 1
    assert store.generated_scene_generation_marker('game', 'turn-1') is not None
    saved = store.generated_scene_snapshot('game', 'turn-1')
    assert saved is not None and saved.success_usage()
    assert store.player_inputs_for('game') == []
    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    with collect_usage() as usage, collect_attempts() as attempts:
        result = asyncio.run(engine.run_turn(turn))
    assert model.calls == 1 and result.model_usage == ()
    assert usage.records == [] and attempts.records == []
    assert len(store.player_inputs_for('game')) == 1
    assert store.event_exists('game', 'turn-1:input:reply:dockhand:spoken')


def test_guarded_commit_ack_lost_recovers_linked_event(tmp_path, monkeypatch):
    class LostCommitAck(SQLiteGameStore):
        lost = False

        def commit(self, *args, **kwargs):
            result = super().commit(*args, **kwargs)
            event = args[2]
            if not self.lost and event is not None and 'generated_scene_snapshot_id' in event.details:
                self.lost = True
                raise OSError('world acknowledgement lost')
            return result

    package = ScenarioPackage.load(EXAMPLE)
    path = tmp_path / 'commit-ack.db'
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    model = CountedOfflineModel()
    with LostCommitAck(path) as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        with pytest.raises(OSError, match='acknowledgement'):
            asyncio.run(engine.run_turn(turn))
        assert model.calls == 1
        assert store.generated_scene_generation_marker('game', 'turn-1') is not None
        assert store.generated_scene_snapshot('game', 'turn-1') is not None
        assert len(store.player_inputs_for('game')) == 1
    with SQLiteGameStore(path) as store:
        engine = TurnEngine(store, package, model,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        monkeypatch.setattr(engine.session.projector, 'project', forbidden)
        monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
        with collect_usage() as usage, collect_attempts() as attempts:
            result = asyncio.run(engine.run_turn(turn))
        assert result.model_usage == () and usage.records == [] and attempts.records == []
        assert model.calls == 1 and len(store.player_inputs_for('game')) == 1
        assert store.event_exists('game', 'turn-1:input:reply:dockhand:spoken')


def test_sqlite_corrupt_marker_row_rejected_before_projection(tmp_path, monkeypatch):
    package = ScenarioPackage.load(EXAMPLE)
    path = tmp_path / 'corrupt-marker.db'
    turn = TurnInput('game', 'Hello', 'turn-1', package.version)
    with SQLiteGameStore(path) as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        marker = GeneratedSceneGenerationMarker.create(game_id='game', turn_id='turn-1',
            **engine.session._bindings(store.load('game'), text_hash(turn.player_text), None))
        store.create_generated_scene_generation_marker(marker)
        store._db.execute('UPDATE generated_scene_generation_markers SET checksum=? '
                          'WHERE game_id=? AND turn_id=?', ('0' * 64, 'game', 'turn-1'))
    with SQLiteGameStore(path) as store:
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
        monkeypatch.setattr(engine.session.projector, 'project', forbidden)
        monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
        with pytest.raises(GeneratedSceneCorrupt):
            engine.inspect_generated_scene_recovery(turn)
        with pytest.raises(GeneratedSceneCorrupt):
            asyncio.run(engine.run_turn(turn))
