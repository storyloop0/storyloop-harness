"""The immutable generation-start marker and its two storage adapters."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
from threading import Barrier

import pytest

from storyloop_harness.advanced import (
    GeneratedSceneCorrupt,
    GeneratedSceneGenerationMarker,
    GeneratedSceneRecovery,
    GeneratedSceneUncertain,
)
from storyloop_harness.core.contracts import Snapshot
from storyloop_harness.core.generated_scene import canonical_json, content_hash, text_hash
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import InMemoryGameStore


def make_marker(**changes):
    body = dict(game_id='game', turn_id='turn', input_hash=text_hash('Hello'),
                base_version=0, base_tick=0, base_state_hash='b' * 64,
                scenario_id='harbor', scenario_version='1', package_fingerprint='c' * 64,
                system_prompt_hash='d' * 64, output_schema_hash='e' * 64,
                commit_config_hash='f' * 64, generation_config_hash='1' * 64)
    return GeneratedSceneGenerationMarker.create(**{**body, **changes})


@pytest.fixture(params=['memory', 'sqlite'])
def store(request, tmp_path):
    value = (InMemoryGameStore() if request.param == 'memory'
             else SQLiteGameStore(tmp_path / 'marker.db'))
    value.create_game(Snapshot('game', 0, 0, {'unicode': '码头'}))
    yield value
    if isinstance(value, SQLiteGameStore):
        value.close()


def encoded_marker(body):
    body['marker_id'] = content_hash({key: value for key, value in body.items()
                                      if key != 'marker_id'})
    encoded = canonical_json(body)
    return encoded, text_hash(encoded)


def test_marker_codec_is_frozen_detached_and_content_addressed():
    marker = make_marker()
    assert GeneratedSceneGenerationMarker.from_storage(
        marker.canonical_json, marker.checksum) == marker
    with pytest.raises(FrozenInstanceError):
        marker.turn_id = 'changed'
    marker.metadata()['input_hash'] = '0' * 64
    assert marker.metadata()['input_hash'] == text_hash('Hello')
    assert marker.marker_id == content_hash({k: v for k, v in marker.metadata().items()
                                            if k != 'marker_id'})
    assert set(marker.metadata()) == {
        'format_version', 'generation_contract_version', 'marker_id', 'game_id', 'turn_id',
        'input_hash', 'base_version', 'base_tick', 'base_state_hash', 'scenario_id',
        'scenario_version', 'package_fingerprint', 'system_prompt_hash',
        'output_schema_hash', 'commit_config_hash', 'generation_config_hash',
    }
    assert str(GeneratedSceneUncertain()) == 'Generated scene outcome is uncertain'


@pytest.mark.parametrize('value', [None, 0, 1, '', 'false', [], {}])
def test_recovery_flag_requires_real_bool(value):
    with pytest.raises(ValueError, match='protect_uncertain_generation'):
        GeneratedSceneRecovery(InMemoryGameStore(), 'offline:v1', value)


def test_recovery_flag_defaults_off_and_accepts_bool():
    store = InMemoryGameStore()
    assert GeneratedSceneRecovery(store, 'offline:v1').protect_uncertain_generation is False
    assert GeneratedSceneRecovery(store, 'offline:v1', True).protect_uncertain_generation is True


def test_marker_claim_is_create_once_and_changes_no_world_data(store):
    before = store.load('game')
    marker = make_marker()
    assert store.generated_scene_generation_marker('game', 'turn') is None
    assert store.create_generated_scene_generation_marker(marker) is True
    assert store.create_generated_scene_generation_marker(marker) is False
    assert store.create_generated_scene_generation_marker(make_marker(input_hash='9' * 64)) is False
    assert store.generated_scene_generation_marker('game', 'turn') == marker
    assert store.create_generated_scene_generation_marker(make_marker(turn_id='turn-2')) is True
    assert store.load('game') == before
    assert store.player_inputs_for('game') == []
    assert store.pending_work('game') == []
    assert store.generated_scene_snapshot('game', 'turn') is None


@pytest.mark.parametrize('key,value', [
    ('format_version', True), ('generation_contract_version', True),
    ('format_version', 99), ('generation_contract_version', 99),
    ('base_version', True), ('base_tick', True), ('base_tick', -1),
    ('base_version', 1.0), ('game_id', ''), ('turn_id', None),
    ('scenario_id', ''), ('scenario_version', 1), ('input_hash', 'G' * 64),
    ('base_state_hash', 'a' * 63), ('generation_config_hash', 1),
])
def test_marker_codec_rejects_semantic_corruption_with_recomputed_hashes(key, value):
    body = make_marker().metadata()
    body[key] = value
    encoded, checksum = encoded_marker(body)
    with pytest.raises(GeneratedSceneCorrupt):
        GeneratedSceneGenerationMarker.from_storage(encoded, checksum)


@pytest.mark.parametrize('key', [
    'input_hash', 'base_state_hash', 'package_fingerprint', 'system_prompt_hash',
    'output_schema_hash', 'commit_config_hash', 'generation_config_hash',
])
def test_each_hash_field_is_validated(key):
    body = make_marker().metadata()
    body[key] = 'A' * 64
    with pytest.raises(GeneratedSceneCorrupt):
        GeneratedSceneGenerationMarker.from_storage(*encoded_marker(body))


def test_marker_codec_rejects_checksum_json_shape_identity_and_encoding():
    marker = make_marker()
    with pytest.raises(GeneratedSceneCorrupt):
        GeneratedSceneGenerationMarker.from_storage(marker.canonical_json, '0' * 64)
    with pytest.raises(GeneratedSceneCorrupt):
        GeneratedSceneGenerationMarker.from_storage('{bad', text_hash('{bad'))
    for change in ('missing', 'extra', 'wrong_id'):
        body = marker.metadata()
        if change == 'missing':
            del body['input_hash']
        elif change == 'extra':
            body['raw_prompt'] = 'forbidden'
        else:
            body['marker_id'] = '0' * 64
            encoded = canonical_json(body)
            with pytest.raises(GeneratedSceneCorrupt):
                GeneratedSceneGenerationMarker.from_storage(encoded, text_hash(encoded))
            continue
        with pytest.raises(GeneratedSceneCorrupt):
            GeneratedSceneGenerationMarker.from_storage(*encoded_marker(body))
    noncanonical = json.dumps(marker.metadata(), indent=2)
    with pytest.raises(GeneratedSceneCorrupt):
        GeneratedSceneGenerationMarker.from_storage(noncanonical, text_hash(noncanonical))


def test_marker_store_rejects_forged_wrapper(store):
    with pytest.raises(GeneratedSceneCorrupt):
        store.create_generated_scene_generation_marker(replace(make_marker(), game_id='other'))
    assert store.generated_scene_generation_marker('game', 'turn') is None


def test_marker_store_rejects_unknown_game_without_evidence(store):
    with pytest.raises(KeyError):
        store.create_generated_scene_generation_marker(make_marker(game_id='missing'))
    assert store.generated_scene_generation_marker('game', 'turn') is None


def test_marker_store_rejects_corrupt_occupied_row_on_read_and_claim(store):
    marker = make_marker()
    assert store.create_generated_scene_generation_marker(marker)
    other = make_marker(turn_id='other')
    if isinstance(store, SQLiteGameStore):
        store._db.execute(
            'UPDATE generated_scene_generation_markers SET canonical_json=?, checksum=? '
            'WHERE game_id=? AND turn_id=?',
            (other.canonical_json, other.checksum, 'game', 'turn'))
    else:
        store._games['game']['generation_markers']['turn'] = (
            other.canonical_json, other.checksum)
    with pytest.raises(GeneratedSceneCorrupt):
        store.generated_scene_generation_marker('game', 'turn')
    with pytest.raises(GeneratedSceneCorrupt):
        store.create_generated_scene_generation_marker(marker)


def test_sqlite_marker_id_column_must_match_body(tmp_path):
    with SQLiteGameStore(tmp_path / 'column.db') as store:
        store.create_game(Snapshot('game', 0, 0, {}))
        marker = make_marker()
        assert store.create_generated_scene_generation_marker(marker)
        store._db.execute(
            'UPDATE generated_scene_generation_markers SET marker_id=? WHERE game_id=? AND turn_id=?',
            ('0' * 64, 'game', 'turn'))
        with pytest.raises(GeneratedSceneCorrupt):
            store.generated_scene_generation_marker('game', 'turn')
        with pytest.raises(GeneratedSceneCorrupt):
            store.create_generated_scene_generation_marker(marker)


def test_sqlite_marker_survives_close_and_reopen(tmp_path):
    path = tmp_path / 'persist.db'
    marker = make_marker()
    with SQLiteGameStore(path) as store:
        store.create_game(Snapshot('game', 0, 0, {}))
        assert store.create_generated_scene_generation_marker(marker)
    with SQLiteGameStore(path) as store:
        assert store.generated_scene_generation_marker('game', 'turn') == marker


@pytest.mark.parametrize('same_binding', [True, False])
def test_two_sqlite_connections_have_one_marker_winner(tmp_path, same_binding):
    path = tmp_path / 'race.db'
    with SQLiteGameStore(path) as seed:
        seed.create_game(Snapshot('game', 0, 0, {}))
    first = make_marker()
    second = first if same_binding else make_marker(input_hash='9' * 64)
    barrier = Barrier(2)

    def claim(marker):
        with SQLiteGameStore(path) as connection:
            barrier.wait(timeout=5)
            return connection.create_generated_scene_generation_marker(marker), marker

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first, second)))
    assert sorted(won for won, _ in results) == [False, True]
    winner = next(marker for won, marker in results if won)
    with SQLiteGameStore(path) as connection:
        assert connection.generated_scene_generation_marker('game', 'turn') == winner
        assert connection.load('game').version == 0
        assert connection.player_inputs_for('game') == []
