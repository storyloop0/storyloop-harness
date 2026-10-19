from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scene_fixtures import a_turn, source_package
from storyloop_harness.agents.scene_schema import SceneTurn
from storyloop_harness.core.generated_scene import (
    GeneratedSceneSnapshot, GeneratedSceneCorrupt, package_fingerprint,
)
from storyloop_harness.world.worldbook import Worldbook
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing.memory import InMemoryGameStore
from storyloop_harness.core.generated_scene import GeneratedSceneConflict
from storyloop_harness.core.generated_scene import text_hash, content_hash, canonical_json
from storyloop_harness.core.contracts import WorldEvent


def make_record(package, **changes):
    fields = dict(game_id='game', turn_id='turn', input_hash=text_hash('Hello'),
        base_version=0, base_tick=0, base_state_hash='b' * 64,
        scenario_id=package.package_id, scenario_version=package.version,
        package_fingerprint=package_fingerprint(package), system_prompt_hash='d' * 64,
        output_schema_hash='e' * 64, commit_config_hash='f' * 64,
        generation_config_hash='1' * 64, projected_request_hash='2' * 64,
        focus_actor_ids=('dockhand',), nearby_actor_ids=('dockhand',),
        preference_hash='3' * 64, scene=SceneTurn.model_validate(a_turn()),
        success_usage=(), usage_coverage='unobserved')
    return GeneratedSceneSnapshot.create(**{**fields, **changes})


def test_snapshot_codec_detaches_and_detects_corruption():
    scene = SceneTurn.model_validate(a_turn())
    record = GeneratedSceneSnapshot.create(
        game_id='game', turn_id='turn', input_hash='a' * 64,
        base_version=0, base_tick=0, base_state_hash='b' * 64,
        scenario_id='harbor', scenario_version='1', package_fingerprint='c' * 64,
        system_prompt_hash='d' * 64, output_schema_hash='e' * 64,
        commit_config_hash='f' * 64, generation_config_hash='1' * 64,
        projected_request_hash='2' * 64, focus_actor_ids=('dockhand',),
        nearby_actor_ids=('dockhand',), preference_hash='3' * 64,
        scene=scene, success_usage=(), usage_coverage='unobserved',
    )
    record.scene().beats[0].text = 'mutated'
    assert record.scene().prose == scene.prose
    record.metadata()['scene']['beats'][0]['text'] = 'mutated'
    assert record.scene().prose == scene.prose
    with pytest.raises(GeneratedSceneCorrupt):
        GeneratedSceneSnapshot.from_storage(record.canonical_json, '0' * 64)
    for mutation in (
        lambda body: body.update(format_version=99),
        lambda body: body.update(usage_coverage='unknown'),
        lambda body: body['scene'].update(prose=''),
    ):
        body = record.metadata()
        mutation(body)
        body['snapshot_id'] = content_hash({k: v for k, v in body.items() if k != 'snapshot_id'})
        encoded = canonical_json(body)
        with pytest.raises(GeneratedSceneCorrupt):
            GeneratedSceneSnapshot.from_storage(encoded, text_hash(encoded))
    with pytest.raises(GeneratedSceneCorrupt):
        GeneratedSceneSnapshot.from_storage('{not json', text_hash('{not json'))


def test_fingerprint_covers_same_version_worldbook_change():
    package = source_package()
    original = package_fingerprint(package)
    entries = list(package.worldbook.entries)
    entries[0] = replace(entries[0], text=entries[0].text + ' changed')
    altered = replace(package, worldbook=Worldbook(package.package_id, package.version, entries))
    assert package_fingerprint(altered) != original
    altered_rule = replace(package, opening=package.opening + ' Changed')
    assert package_fingerprint(altered_rule) != original
    assert package_fingerprint(source_package()) == original


@pytest.mark.parametrize('store_type', (SQLiteGameStore, InMemoryGameStore))
def test_store_insert_once_and_no_world_change(tmp_path, store_type):
    package = source_package()
    store = store_type(tmp_path / 'scene.db') if store_type is SQLiteGameStore else store_type()
    try:
        package.seed_game(store, 'game')
        before = store.load('game')
        record = make_record(package)
        assert store.save_generated_scene_snapshot(record) == record
        assert store.save_generated_scene_snapshot(record) == record
        assert store.generated_scene_snapshot('game', 'turn') == record
        assert store.load('game') == before
        assert store.player_inputs_for('game') == []
        with pytest.raises(GeneratedSceneConflict):
            store.save_generated_scene_snapshot(make_record(package, input_hash='9' * 64))
    finally:
        if hasattr(store, 'close'):
            store.close()


@pytest.mark.parametrize('store_type', (SQLiteGameStore, InMemoryGameStore))
def test_event_reference_is_checked_atomically(tmp_path, store_type):
    package = source_package()
    store = store_type(tmp_path / 'refs.db') if store_type is SQLiteGameStore else store_type()
    try:
        package.seed_game(store, 'game')
        record = make_record(package)
        details = dict(text='Hello', single_call=record.scene().model_dump(),
                       generated_scene_snapshot_id=record.snapshot_id,
                       generated_scene_snapshot_checksum=record.checksum)
        event = WorldEvent('turn:input', 'player_input', 'player', None, 1, (), details)
        with pytest.raises(GeneratedSceneCorrupt):
            store.commit('game', 0, event, (), ())
        store.save_generated_scene_snapshot(record)
        bad = WorldEvent('turn:input', 'player_input', 'player', None, 1, (),
                         {**details, 'generated_scene_snapshot_checksum': '0' * 64})
        with pytest.raises(GeneratedSceneCorrupt):
            store.commit('game', 0, bad, (), ())
        assert store.load('game').version == 0
        store.commit('game', 0, event, (), ())
        assert store.load('game').version == 1
    finally:
        if hasattr(store, 'close'):
            store.close()


def test_two_sqlite_connections_cannot_replace_snapshot(tmp_path):
    package = source_package()
    path = tmp_path / 'race.db'
    with SQLiteGameStore(path) as seed:
        package.seed_game(seed, 'game')
    records = (make_record(package), make_record(package, input_hash='9' * 64))
    barrier = Barrier(2)

    def save(record):
        with SQLiteGameStore(path) as store:
            barrier.wait(timeout=5)
            try:
                return store.save_generated_scene_snapshot(record)
            except GeneratedSceneConflict:
                return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, records))
    assert results.count('conflict') == 1
    with SQLiteGameStore(path) as store:
        assert store.generated_scene_snapshot('game', 'turn') in records
        assert store.load('game').version == 0


def test_sqlite_corrupt_row_fails_closed(tmp_path):
    package = source_package()
    with SQLiteGameStore(tmp_path / 'corrupt.db') as store:
        package.seed_game(store, 'game')
        store.save_generated_scene_snapshot(make_record(package))
        store._db.execute("UPDATE generated_scene_snapshots SET checksum=? WHERE game_id=? AND turn_id=?",
                          ('0' * 64, 'game', 'turn'))
        with pytest.raises(GeneratedSceneCorrupt):
            store.generated_scene_snapshot('game', 'turn')
