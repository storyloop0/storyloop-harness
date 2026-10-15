"""Public, read-only inspection of durable generated scenes."""

import asyncio
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.advanced import (
    GeneratedSceneRecovery, GeneratedSceneCorrupt, GeneratedSceneMismatch,
    GeneratedSceneStale, snapshot_state_fingerprint,
)
from storyloop_harness.core.contracts import Snapshot, WorldEvent
from storyloop_harness.testing import InMemoryGameStore, OfflineModel
from storyloop_harness.world.worldbook import Worldbook


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/freeform'


class FailCommit(InMemoryGameStore):
    fail = True

    def commit(self, *args, **kwargs):
        if self.fail and args[2] is not None:
            self.fail = False
            raise RuntimeError('injected commit failure')
        return super().commit(*args, **kwargs)


def setup(*, committed=False):
    package = ScenarioPackage.load(EXAMPLE)
    store = InMemoryGameStore() if committed else FailCommit()
    package.seed_game(store, 'game')
    turn = TurnInput('game', '你好，码头', 'turn-1', package.version)
    engine = TurnEngine(store, package, OfflineModel(),
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
    if committed:
        asyncio.run(engine.run_turn(turn))
    else:
        with pytest.raises(RuntimeError, match='injected commit failure'):
            asyncio.run(engine.run_turn(turn))
    return package, store, turn, engine


def inspect_both(engine, turn, *, max_tick=None):
    before = engine.store.load(turn.game_id)
    work_before = engine.store.pending_work(turn.game_id)
    a = engine.inspect_generated_scene_recovery(turn, max_tick=max_tick)
    b = engine.inspect_generated_scene_recovery_fingerprint(
        turn.game_id, turn.turn_id, sha256(turn.player_text.encode('utf-8')).hexdigest(),
        turn.scenario_version, max_tick=max_tick)
    assert a == b
    assert engine.store.load(turn.game_id) == before
    assert engine.store.pending_work(turn.game_id) == work_before
    return a


@pytest.mark.parametrize('committed', [False, True])
def test_inspection_returns_saved_scene_without_work(committed, monkeypatch):
    _, store, turn, engine = setup(committed=committed)
    saved = store.generated_scene_snapshot(turn.game_id, turn.turn_id)

    def forbidden(*args, **kwargs):
        raise AssertionError('inspection triggered work')

    monkeypatch.setattr(engine.session.projector, 'project', forbidden)
    monkeypatch.setattr(engine.session.generator, 'generate', forbidden)
    monkeypatch.setattr(engine.session, '_runner', forbidden)
    monkeypatch.setattr(store, 'commit', forbidden)
    monkeypatch.setattr(store, 'save_generated_scene_snapshot', forbidden)
    inspection = inspect_both(engine, turn)
    assert inspection.snapshot == saved
    assert inspection.committed is committed


def test_missing_snapshot_returns_none_and_disabled_recovery_rejects():
    package = ScenarioPackage.load(EXAMPLE)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    turn = TurnInput('game', '你好', 'turn-1', package.version)
    engine = TurnEngine(store, package, OfflineModel(),
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
    assert inspect_both(engine, turn) is None
    disabled = TurnEngine(store, package, OfflineModel())
    with pytest.raises(ValueError, match='configured'):
        disabled.inspect_generated_scene_recovery(turn)
    with pytest.raises(ValueError, match='configured'):
        disabled.inspect_generated_scene_recovery_fingerprint('game', 'turn-1', 'a' * 64,
                                                               package.version)


@pytest.mark.parametrize('bad_hash', ['A' * 64, 'a' * 63, 'g' * 64, 123, None])
def test_fingerprint_rejects_invalid_hash(bad_hash):
    package = ScenarioPackage.load(EXAMPLE)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    engine = TurnEngine(store, package, OfflineModel(),
        generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
    with pytest.raises(ValueError, match='fingerprint'):
        engine.inspect_generated_scene_recovery_fingerprint('game', 'turn-1', bad_hash,
                                                               package.version)


def test_inspection_detects_input_package_config_and_base_mismatch():
    package, store, turn, engine = setup()
    with pytest.raises(GeneratedSceneMismatch):
        engine.inspect_generated_scene_recovery(replace(turn, player_text='changed'))
    with pytest.raises(ValueError, match='scenario version'):
        engine.inspect_generated_scene_recovery(replace(turn, scenario_version='other'))
    with pytest.raises(GeneratedSceneMismatch):
        engine.inspect_generated_scene_recovery(turn, max_tick=4)
    with pytest.raises(GeneratedSceneMismatch):
        TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v2'))\
            .inspect_generated_scene_recovery(turn)
    original = package.worldbook.entries[0]
    changed_book = Worldbook(package.package_id, package.version, [
        replace(original, text=original.text + ' Revised.'), *package.worldbook.entries[1:]])
    with pytest.raises(GeneratedSceneMismatch):
        TurnEngine(store, replace(package, worldbook=changed_book), OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))\
            .inspect_generated_scene_recovery(turn)
    store.commit('game', 0, WorldEvent('other', 'test', None, None, 0, ()), (), ())
    with pytest.raises(GeneratedSceneStale):
        engine.inspect_generated_scene_recovery(turn)


def test_inspection_rejects_corrupt_checksum_wrapper_and_event_reference():
    _, store, turn, engine = setup(committed=True)
    row = store._games['game']['snapshots']['turn-1']
    store._games['game']['snapshots']['turn-1'] = (row[0], '0' * 64)
    with pytest.raises(GeneratedSceneCorrupt):
        engine.inspect_generated_scene_recovery(turn)
    store._games['game']['snapshots']['turn-1'] = row
    event_version, event = store._games['game']['events']['turn-1:input']
    store._games['game']['events']['turn-1:input'] = (event_version, replace(event,
        details={**event.details, 'generated_scene_snapshot_checksum': '0' * 64}))
    with pytest.raises(GeneratedSceneCorrupt):
        engine.inspect_generated_scene_recovery(turn)


def test_inspection_rejects_wrapper_identity_and_missing_referenced_snapshot(monkeypatch):
    _, store, turn, engine = setup(committed=True)
    record = store.generated_scene_snapshot(turn.game_id, turn.turn_id)
    wrong_wrapper = replace(record, game_id='other')
    monkeypatch.setattr(store, 'generated_scene_snapshot', lambda *args: wrong_wrapper)
    with pytest.raises(GeneratedSceneCorrupt):
        engine.inspect_generated_scene_recovery(turn)
    monkeypatch.setattr(store, 'generated_scene_snapshot', lambda *args: None)
    with pytest.raises(GeneratedSceneCorrupt):
        engine.inspect_generated_scene_recovery(turn)


def test_inspection_checks_saved_scenario_identity():
    package, store, turn, engine = setup()
    before = store.load('game')
    changed = dict(before.data)
    changed['scenario'] = {**changed['scenario'], 'id': 'other'}
    store._games['game']['snapshot'] = replace(before, data=changed)
    with pytest.raises(ValueError, match='saved scenario'):
        engine.inspect_generated_scene_recovery(turn)


def test_public_state_fingerprint_hashes_only_nested_unicode_world_data():
    data = {'人物': {'名字': '阿青', '关系': ['朋友', {'分数': 2}]}}
    first = Snapshot('game', 0, 0, data)
    second = Snapshot('other', 9, 30, data)
    assert snapshot_state_fingerprint(first) == snapshot_state_fingerprint(second)
    _, store, turn, _ = setup()
    saved = store.generated_scene_snapshot(turn.game_id, turn.turn_id)
    assert saved.metadata()['base_state_hash'] == snapshot_state_fingerprint(store.load('game'))
