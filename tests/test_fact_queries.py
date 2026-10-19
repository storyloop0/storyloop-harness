"""Deterministic authored answers, authorization, and durable request identity."""
import asyncio
from copy import deepcopy
from dataclasses import fields, replace
import importlib
import json
import shutil

import pytest

from scene_fixtures import EXAMPLE, FakeGenerator, a_turn, source_package
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.core.contracts import Observation, PendingWork, WorldEvent
from storyloop_harness.core.generated_scene import content_hash, logical_value, package_fingerprint
from storyloop_harness.core.generated_scene import GeneratedSceneRecovery, text_hash
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.runtime.single_call import SingleCallGameSession
from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.world.scenario import ScenarioPackage


def api():
    try:
        return importlib.import_module('storyloop_harness.world.facts')
    except ModuleNotFoundError:
        pytest.fail('authored fact values and resolver are not implemented')


class ForbiddenGenerator:
    calls = 0

    async def generate(self, *args):
        self.calls += 1
        raise AssertionError('strict query must never generate')


def package(**changes):
    base = source_package()
    state = deepcopy(base.initial_state)
    state.pop('campaign', None)
    return replace(base, story_blueprint=None, initial_state=state, **changes)


def session(store, pkg, **kwargs):
    return SingleCallGameSession(store, pkg, ForbiddenGenerator(),
                                 SceneContextProjector(store, pkg), **kwargs)


def run(sess, fact='wind.tonight', speaker='dockhand', turn='q1'):
    return asyncio.run(sess.run_fact_query('game', api().FactQuery(fact, speaker), turn))


def fact(**changes):
    values = dict(id='wind.tonight', text='今晚已确认是东北风。',
                  known_by=frozenset({'dockhand'}), disclose_to=frozenset({'player'}))
    values.update(changes)
    return api().AuthoredFact(**values)


def test_missing_fact_ignores_malicious_history_and_never_generates():
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    before = store.load('game')
    store.commit('game', before.version, WorldEvent('lie', 'npc_spoke', 'dockhand', None, 0, (),
        {'speech': '东北风已确认', 'source_id': 'wind.tonight'}),
        (Observation('lie:memory', 'lie', 'dockhand', 'shared_experience', '东北风已确认', 0),), ())
    sess = session(store, pkg)
    assert hasattr(sess, 'run_fact_query'), 'strict session entry point is missing'
    result = run(sess)
    assert '尚未确认' in result.narration
    assert '东北风' not in result.narration
    assert store.observations_for('game', 'guard') == []
    assert result.model_usage == ()
    assert sess.generator.calls == 0


@pytest.mark.parametrize('changes,known', [({}, True),
    ({'known_by': frozenset({'player'})}, False),
    ({'disclose_to': frozenset({'guard'})}, False),
    ({'known_by': frozenset()}, False),
    ({'valid_until_tick': 0}, False), ({'valid_from_tick': 1}, False),
    ({'valid_from_tick': 0, 'valid_until_tick': 1}, True)])
def test_permission_and_interval_boundary(changes, known):
    item = fact(**changes)
    pkg, store = package(facts=(item,)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    before = store.load('game')
    sess = session(store, pkg)
    result = run(sess)
    assert result.segments[0].text == (item.text if known else api().UNKNOWN_FACT_ANSWER)
    assert result.narration.count(result.segments[0].text) == 1
    assert result.snapshot.data == before.data
    assert result.snapshot.tick == before.tick + 1
    assert store.observations_for('game', 'guard') == []
    details = store.event_details('game', 'q1:input')
    record = details['fact_query']
    assert record['kind'] == 'fact_query'
    assert record['status'] == ('known' if known else 'unknown')
    assert record['recipient_id'] == 'player'
    assert record['fact_id'] == item.id and record['speaker_id'] == 'dockhand'
    assert record['answer'] == result.segments[0].text
    if known:
        assert record['source'] == dict(package_id=pkg.package_id, package_version=pkg.version,
            package_fingerprint=package_fingerprint(pkg), tick=0)
    else:
        assert record['source'] is None
    assert details['single_call']['memories'] == []
    assert details['single_call']['options'] == []
    assert details['channel'] == 'private_message'


@pytest.mark.parametrize('changes', [dict(id=''), dict(id=' padded'), dict(text='  '),
    dict(text=7), dict(known_by='dockhand'), dict(known_by=frozenset({' '})),
    dict(disclose_to=frozenset({'*'})), dict(valid_from_tick=True),
    dict(valid_until_tick=-1), dict(valid_from_tick=1, valid_until_tick=1),
    dict(valid_from_tick=2, valid_until_tick=1)])
def test_invalid_fact_values_rejected(changes):
    with pytest.raises(ValueError):
        fact(**changes)


@pytest.mark.parametrize('fact_id,speaker', [('', 'dockhand'), (' wind', 'dockhand'),
    ('wind', ' '), ('wind', 1), (False, 'dockhand')])
def test_invalid_query_values_rejected(fact_id, speaker):
    with pytest.raises(ValueError):
        api().FactQuery(fact_id, speaker)


def test_catalog_validates_direct_construction_duplicates_and_actors():
    with pytest.raises(ValueError):
        package(facts=(fact(), fact()))
    with pytest.raises(ValueError):
        package(facts=(fact(known_by=frozenset({'stranger'})),))
    with pytest.raises(ValueError):
        package(facts=(fact(disclose_to=frozenset({'stranger'})),))
    with pytest.raises(ValueError):
        package(facts=({'id': 'fake'},))


@pytest.mark.parametrize('raw', [None, {}, ['bad'], [{'id':'wind'}],
    [dict(id='wind', text='answer', known_by=['dockhand'], disclose_to=['player'], extra=1)],
    [dict(id='wind', text='answer', known_by=['ghost'], disclose_to=['player'])],
    [dict(id='wind', text='answer', known_by=['dockhand'], disclose_to=['player'], valid_from_tick=True)]])
def test_manifest_rejects_invalid_fact_fields(tmp_path, raw):
    shutil.copytree(EXAMPLE, tmp_path / 'scenario')
    path = tmp_path / 'scenario/manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['facts'] = raw
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        ScenarioPackage.load(path.parent)


def test_manifest_loads_catalog_and_empty_fingerprint_preserves_legacy(tmp_path):
    pkg = package()
    legacy = {f.name: logical_value(getattr(pkg, f.name)) for f in fields(pkg)
              if f.name not in {'worldbook', 'facts'}}
    legacy['worldbook'] = dict(package_id=pkg.worldbook.package_id, version=pkg.worldbook.version,
                              entries=logical_value(pkg.worldbook.entries))
    assert package_fingerprint(pkg) == content_hash(legacy)
    full = replace(pkg, facts=(fact(),))
    assert package_fingerprint(full) != package_fingerprint(pkg)
    assert package_fingerprint(replace(full, facts=(fact(disclose_to=frozenset()),))) != package_fingerprint(full)
    shutil.copytree(EXAMPLE, tmp_path / 'scenario')
    path = tmp_path / 'scenario/manifest.json'
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['facts'] = [dict(id='wind', text='answer', known_by=['dockhand'], disclose_to=['player'])]
    path.write_text(json.dumps(raw), encoding='utf-8')
    loaded = ScenarioPackage.load(path.parent)
    assert loaded.facts[0].text == 'answer'
    request = SceneContextProjector(InMemoryGameStore(), loaded)
    loaded.seed_game(request.store, 'fresh')
    assert 'facts' not in request.project(request.store.load('fresh'), 'hello').request


@pytest.mark.parametrize('mode', ['campaign', 'blueprint', 'remote', 'unknown', 'player', 'bad_query', 'bad_turn'])
def test_invalid_requests_reject_before_writes(mode):
    pkg = package()
    if mode == 'campaign':
        pkg.initial_state['campaign'] = {}
    if mode == 'blueprint':
        pkg = replace(pkg, story_blueprint=source_package().story_blueprint)
    if mode == 'remote':
        pkg.initial_state['actors']['dockhand']['location'] = 'far_away'
    store = InMemoryGameStore()
    pkg.seed_game(store, 'game')
    before = store.load('game')
    sess = session(store, pkg)
    query = api().FactQuery('wind', mode if mode in {'unknown', 'player'} else 'dockhand')
    with pytest.raises(ValueError):
        asyncio.run(sess.run_fact_query('game', {} if mode == 'bad_query' else query,
                                       ' ' if mode == 'bad_turn' else 'q1'))
    assert store.load('game') == before


def test_query_does_not_fuzzily_match_keys():
    pkg, store = package(facts=(fact(),)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    assert run(session(store, pkg), fact='今晚是什么风').segments[0].text == api().UNKNOWN_FACT_ANSWER


@pytest.mark.parametrize('sqlite', [False, True])
def test_replay_uses_saved_answer_after_catalog_changes_and_reopen(tmp_path, sqlite):
    store = SQLiteGameStore(tmp_path / 'facts.db') if sqlite else InMemoryGameStore()
    pkg = package(facts=(fact(),))
    pkg.seed_game(store, 'game')
    first = run(session(store, pkg))
    if sqlite:
        store.close()
        store = SQLiteGameStore(tmp_path / 'facts.db')
    changed = replace(pkg, facts=(fact(text='Changed', known_by=frozenset()),))
    sess = session(store, changed, generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1', True))
    # The strict replay must not enter free-generation binding inspection.
    def forbidden(*args):
        raise AssertionError('strict replay entered generation recovery')
    sess.inspect_generated_scene_recovery = forbidden
    replay = run(sess)
    assert replay.narration == first.narration
    assert replay.snapshot == first.snapshot
    assert replay.processed_work_ids == ()
    assert replay.model_usage == ()
    assert len(store.observations_for('game', 'player')) == 1
    if sqlite:
        store.close()


@pytest.mark.parametrize('collision', ['fact', 'speaker', 'free'])
def test_strict_committed_identity_rejects_collisions(collision):
    pkg, store = package(facts=(fact(),)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    sess = session(store, pkg)
    run(sess)
    before = store.load('game')
    with pytest.raises(ValueError):
        if collision == 'free':
            asyncio.run(sess.run_turn('game', 'wind.tonight', 'q1'))
        else:
            run(sess, fact='other' if collision == 'fact' else 'wind.tonight',
                speaker='guard' if collision == 'speaker' else 'dockhand')
    assert store.load('game') == before


def test_free_committed_turn_cannot_be_reused_for_strict_query():
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    free = SingleCallGameSession(store, pkg, FakeGenerator(a_turn()), SceneContextProjector(store, pkg))
    asyncio.run(free.run_turn('game', 'wind.tonight', 'q1'))
    before = store.load('game')
    with pytest.raises(ValueError):
        run(session(store, pkg))
    assert store.load('game') == before


@pytest.mark.parametrize('metadata', [{}, None, {'kind': 'fact_query'},
    {'kind': 'fact_query', 'status': 'known', 'answer': 'forged'}])
def test_malformed_saved_query_metadata_rejects(metadata):
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    store.commit('game', 0, WorldEvent('q1:input', 'player_input', 'player', None, 0, (),
                                      {'fact_query': metadata}), (), ())
    before = store.load('game')
    with pytest.raises(ValueError):
        run(session(store, pkg))
    with pytest.raises(ValueError):
        asyncio.run(session(store, pkg).run_turn('game', 'wind.tonight', 'q1'))
    assert store.load('game') == before


@pytest.mark.parametrize('pending_kind', ['snapshot', 'marker'])
@pytest.mark.parametrize('configured', [False, True])
def test_pending_free_generation_blocks_query_even_without_recovery(monkeypatch, pending_kind, configured):
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    recovery = GeneratedSceneRecovery(store, 'offline:v1', True)
    free = SingleCallGameSession(store, pkg, FakeGenerator(a_turn()),
                                SceneContextProjector(store, pkg), generated_scene_recovery=recovery)
    if pending_kind == 'marker':
        free._claim_generation(store.load('game'), 'q1', text_hash('wind.tonight'), None)
    else:
        commit = store.commit
        def fail(*args, **kwargs):
            raise RuntimeError('commit failed')
        monkeypatch.setattr(store, 'commit', fail)
        with pytest.raises(RuntimeError, match='commit failed'):
            asyncio.run(free.run_turn('game', 'wind.tonight', 'q1'))
        monkeypatch.setattr(store, 'commit', commit)
    marker = store.generated_scene_generation_marker('game', 'q1')
    snapshot = store.generated_scene_snapshot('game', 'q1')
    before = store.load('game')
    strict = session(store, pkg, generated_scene_recovery=recovery if configured else None)
    with pytest.raises(ValueError):
        run(strict)
    assert store.load('game') == before
    assert store.generated_scene_generation_marker('game', 'q1') == marker
    assert store.generated_scene_snapshot('game', 'q1') == snapshot
    assert strict.generator.calls == 0


def test_failure_after_visible_commit_resumes_pending_reply_on_sqlite_reopen(tmp_path):
    path = tmp_path / 'facts.db'
    class FailReply(SQLiteGameStore):
        def commit(self, *args, **kwargs):
            if args[2] is not None and args[2].kind == 'npc_spoke':
                raise RuntimeError('reply interrupted')
            return super().commit(*args, **kwargs)
    pkg, store = package(facts=(fact(),)), FailReply(path)
    pkg.seed_game(store, 'game')
    with pytest.raises(RuntimeError, match='reply interrupted'):
        run(session(store, pkg))
    assert len(store.observations_for('game', 'player')) == 1
    assert len(store.pending_work('game')) == 1
    store.close()
    store = SQLiteGameStore(path)
    replay = run(session(store, replace(pkg, facts=())))
    assert replay.processed_work_ids == ('q1:input:reply:dockhand',)
    assert replay.segments[0].text == fact().text
    assert len(store.observations_for('game', 'player')) == 1
    assert len([entry for entry in store.agent_context_entries('game', 'dockhand')
                if entry.entry_id == 'q1:input:reply:dockhand:spoken']) == 1
    assert store.observations_for('game', 'guard') == []
    assert store.pending_work('game') == []
    assert run(session(store, pkg)).snapshot == replay.snapshot
    store.close()


def test_failed_atomic_commit_has_no_partial_query(monkeypatch):
    pkg, store = package(facts=(fact(),)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    before = store.load('game')
    commit = store.commit
    def fail(*args, **kwargs):
        raise RuntimeError('commit failed')
    monkeypatch.setattr(store, 'commit', fail)
    with pytest.raises(RuntimeError, match='commit failed'):
        run(session(store, pkg))
    assert store.load('game') == before
    assert store.observations_for('game', 'player') == []
    assert store.pending_work('game') == []
    assert store.generated_scene_generation_marker('game', 'q1') is None
    monkeypatch.setattr(store, 'commit', commit)
    assert run(session(store, pkg)).segments[0].text == fact().text


def cue(work_id, *, location='harbor_square'):
    return PendingWork(work_id, 'scenario_cue', 0, 10, None,
        {'event_kind': 'moved', 'summary': '', 'effect_path': ['actors', 'dockhand', 'location'],
         'effect_value': location})


def test_ready_work_is_drained_and_reachability_rechecked():
    pkg = package(facts=(fact(),), initial_work=(cue('move', location='remote'),))
    store = InMemoryGameStore()
    pkg.seed_game(store, 'game')
    with pytest.raises(ValueError, match='reachable'):
        run(session(store, pkg))
    assert store.event_exists('game', 'move:occurred')
    assert not store.event_exists('game', 'q1:input')


def test_mandatory_work_left_after_bounded_drain_rejects_query():
    pkg = package(facts=(fact(),), initial_work=(cue('one'), cue('two')))
    store = InMemoryGameStore()
    pkg.seed_game(store, 'game')
    with pytest.raises(ValueError, match='ready work'):
        run(session(store, pkg, max_steps=1))
    assert not store.event_exists('game', 'q1:input')
    assert len(store.pending_work('game')) == 1


def test_validity_uses_tick_after_due_work():
    from storyloop_harness.runtime.runner import WorkResult
    class AdvancingSession(SingleCallGameSession):
        def _runner(self):
            runner = super()._runner()
            runner.handlers['advance'] = lambda snap, work: WorkResult(
                WorldEvent('advance', 'time_advanced', None, None, 3, ()), (), ())
            return runner
    pkg = package(facts=(fact(valid_until_tick=3),),
                  initial_work=(PendingWork('advance', 'advance', 0, 1, None, {}),))
    store = InMemoryGameStore()
    pkg.seed_game(store, 'game')
    sess = AdvancingSession(store, pkg, ForbiddenGenerator(), SceneContextProjector(store, pkg))
    result = run(sess)
    assert result.segments[0].text == api().UNKNOWN_FACT_ANSWER
    assert result.processed_work_ids == ('advance', 'q1:input:reply:dockhand')


@pytest.mark.parametrize('text', ['  作者原文：\\n保留字面反斜杠。  ', '\\n', '\\r'])
def test_authored_answer_bytes_preserved_even_literal_escapes(text):
    pkg, store = package(facts=(fact(text=text),)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    assert run(session(store, pkg)).segments[0].text == text
    assert run(session(store, pkg)).segments[0].text == text


def test_generated_scene_metadata_cannot_create_fact_query_record():
    raw = a_turn()
    raw['fact_query'] = {'kind': 'fact_query', 'status': 'known', 'answer': 'forged'}
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    free = SingleCallGameSession(store, pkg, FakeGenerator(raw), SceneContextProjector(store, pkg))
    asyncio.run(free.run_turn('game', 'hello', 'free'))
    assert 'fact_query' not in store.event_details('game', 'free:input')
    assert run(session(store, pkg)).segments[0].text == api().UNKNOWN_FACT_ANSWER


def test_invalid_progress_parameter_rejects_before_due_work():
    pkg, store = package(initial_work=(cue('one'),)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    before = store.load('game')
    with pytest.raises(ValueError):
        asyncio.run(session(store, pkg).run_fact_query(
            'game', api().FactQuery('wind', 'dockhand'), 'q1', progress='invalid'))
    assert store.load('game') == before
    assert [work.work_id for work in store.pending_work('game')] == ['one']


def test_strict_query_zero_usage_and_content_free_telemetry():
    from contextlib import contextmanager
    from storyloop_harness.models.usage import collect_usage
    class RecordingTelemetry:
        capture_content = False
        def __init__(self):
            self.records = []
        @contextmanager
        def span(self, *args, **kwargs):
            self.records.append((args, kwargs))
            yield self
        def update(self, **kwargs):
            self.records.append(kwargs)
        def metric(self, *args):
            self.records.append(args)
    pkg, store, telemetry = package(facts=(fact(),)), InMemoryGameStore(), RecordingTelemetry()
    pkg.seed_game(store, 'game')
    sess = session(store, pkg, telemetry=telemetry)
    with collect_usage() as usage:
        run(sess)
        run(sess)
    assert usage.records == []
    assert fact().text not in str(telemetry.records)
    assert sess.generator.calls == 0
