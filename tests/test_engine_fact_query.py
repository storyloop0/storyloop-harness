"""Public strict fact-query boundary and its commit isolation."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from storyloop_harness import AuthoredFact, FactQuery, FactQueryInput, ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.core.contracts import PendingWork, Snapshot
from storyloop_harness.core.generated_scene import GeneratedSceneRecovery, text_hash
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import InMemoryGameStore, OfflineModel
from storyloop_harness.world.facts import UNKNOWN_FACT_ANSWER


class ForbiddenModel:
    calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('fact query invoked scene model')


def package(**changes):
    base = ScenarioPackage.load(Path(__file__).parents[1] / 'examples/freeform')
    fact = AuthoredFact('weather.confirmed', '北风已确认。', frozenset({'dockhand'}),
                        frozenset({'player'}))
    return replace(base, **(dict(initial_work=(), facts=(fact,)) | changes))


def request(pkg, *, fact='weather.confirmed', speaker='dockhand', turn='query-1', version=None):
    return FactQueryInput('game', FactQuery(fact, speaker), turn,
                          pkg.version if version is None else version)


def run(engine, value, **kwargs):
    return asyncio.run(engine.run_fact_query(value, **kwargs))


def test_known_query_is_public_and_replay_has_no_new_commits():
    pkg, store, model = package(), InMemoryGameStore(), ForbiddenModel()
    pkg.seed_game(store, 'game')
    engine = TurnEngine(store, pkg, model)
    first = run(engine, request(pkg))
    assert first.segments[0].text == pkg.facts[0].text
    assert first.events and all(store.event_exists('game', event.event_id) for event in first.events)
    assert first.snapshot == store.load('game')
    assert first.model_usage == () and model.calls == 0
    replay = run(engine, request(pkg))
    assert replay.narration == first.narration
    assert replay.snapshot == first.snapshot
    assert replay.events == () and replay.model_usage == ()


@pytest.mark.parametrize('fact,expected', [
    ('weather.missing', UNKNOWN_FACT_ANSWER),
    ('weather.confirmed', '北风已确认。'),
])
def test_missing_and_known_answers(fact, expected):
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    assert run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg, fact=fact)).segments[0].text == expected


@pytest.mark.parametrize('change', [
    {'known_by': frozenset({'player'})}, {'disclose_to': frozenset()},
    {'valid_until_tick': 0}, {'valid_from_tick': 1},
])
def test_private_unaware_and_out_of_window_are_uniformly_unknown(change):
    base = package()
    pkg = replace(base, facts=(replace(base.facts[0], **change),))
    store = InMemoryGameStore()
    pkg.seed_game(store, 'game')
    assert run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg)).segments[0].text == UNKNOWN_FACT_ANSWER


@pytest.mark.parametrize('field,value', [
    ('game_id', ' '), ('turn_id', ''), ('scenario_version', 'bad version'),
    ('query', {}), ('query', FactQuery('weather.confirmed', 'dockhand')),
])
def test_invalid_boundary_values_precede_pending_work(field, value):
    pkg, store = package(initial_work=(PendingWork('due', 'scenario_cue', 0, 10, None,
        {'event_kind': 'moved', 'summary': '', 'effect_path': ['actors', 'dockhand', 'location'],
         'effect_value': 'remote'}),)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    before, pending = store.load('game'), store.pending_work('game')
    entry = request(pkg)
    if field == 'query':
        if not isinstance(value, dict):
            object.__setattr__(value, 'fact_id', 'bad fact')
    with pytest.raises(ValueError):
        run(TurnEngine(store, pkg, ForbiddenModel()), replace(entry, **{field: value}))
    assert store.load('game') == before and store.pending_work('game') == pending


def test_invalid_input_and_progress_and_version_precede_work():
    cue = PendingWork('due', 'scenario_cue', 0, 10, None,
        {'event_kind': 'moved', 'summary': '', 'effect_path': ['actors', 'dockhand', 'location'],
         'effect_value': 'remote'})
    pkg, store = package(initial_work=(cue,)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    engine, before, pending = TurnEngine(store, pkg, ForbiddenModel()), store.load('game'), store.pending_work('game')
    for entry, progress in (({}, None), (request(pkg), 'bad'),
                            (request(pkg, version='other'), None)):
        with pytest.raises(ValueError):
            run(engine, entry, progress=progress)
    assert store.load('game') == before
    assert store.pending_work('game') == pending


@pytest.mark.parametrize('field,value', [('id', 'other'), ('version', 'other')])
def test_saved_identity_mismatch_rejects_before_work(field, value):
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    before = store.load('game')
    identity = before.data['scenario'] | {field: value}
    store._games['game']['snapshot'] = Snapshot(before.game_id, before.version, before.tick,
        {**before.data, 'scenario': identity})
    altered = store.load('game')
    with pytest.raises(ValueError, match='saved scenario'):
        run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg))
    assert store.load('game') == altered


def test_program_blueprint_and_campaign_rejected():
    from types import SimpleNamespace
    from scene_fixtures import source_package
    base = package()
    for mode in ('program', 'blueprint', 'campaign'):
        pkg = replace(base, story_blueprint=source_package().story_blueprint) if mode == 'blueprint' else base
        store = InMemoryGameStore()
        pkg.seed_game(store, 'game')
        if mode == 'campaign':
            before = store.load('game')
            store._games['game']['snapshot'] = Snapshot(before.game_id, before.version, before.tick,
                {**before.data, 'campaign': {}})
        before = store.load('game')
        engine = TurnEngine(store, pkg, ForbiddenModel(),
                            program=SimpleNamespace() if mode == 'program' else None)
        with pytest.raises(ValueError):
            run(engine, request(pkg))
        assert store.load('game') == before


@pytest.mark.parametrize('collision', ['fact', 'speaker', 'free'])
def test_committed_query_identity_collision(collision):
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    engine = TurnEngine(store, pkg, ForbiddenModel())
    run(engine, request(pkg))
    before = store.load('game')
    with pytest.raises(ValueError):
        if collision == 'free':
            asyncio.run(engine.run_turn(TurnInput('game', 'hello', 'query-1', pkg.version)))
        else:
            run(engine, request(pkg, **({'fact': 'other'} if collision == 'fact' else {'speaker': 'guard'})))
    assert store.load('game') == before


def test_sqlite_reopen_and_replay(tmp_path):
    pkg, path = package(), tmp_path / 'game.sqlite3'
    with SQLiteGameStore(path) as store:
        pkg.seed_game(store, 'game')
        first = run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg))
    with SQLiteGameStore(path) as store:
        changed_catalog = replace(pkg, facts=())
        second = run(TurnEngine(store, changed_catalog, ForbiddenModel()), request(changed_catalog))
        assert second.narration == first.narration
        assert second.events == () and second.snapshot == first.snapshot


def test_interrupted_reply_continues_through_engine_after_sqlite_reopen(tmp_path):
    class FailReply(SQLiteGameStore):
        def commit(self, *args, **kwargs):
            if args[2] is not None and args[2].kind == 'npc_spoke':
                raise RuntimeError('reply interrupted')
            return super().commit(*args, **kwargs)

    pkg, path = package(), tmp_path / 'interrupted.sqlite3'
    with FailReply(path) as store:
        pkg.seed_game(store, 'game')
        with pytest.raises(RuntimeError, match='reply interrupted'):
            run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg))
        assert store.event_exists('game', 'query-1:input')
        assert len(store.pending_work('game')) == 1
    with SQLiteGameStore(path) as store:
        resumed = run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg))
        assert resumed.segments[0].text == pkg.facts[0].text
        assert resumed.processed_work_ids == ('query-1:input:reply:dockhand',)
        assert len(resumed.events) == 1 and resumed.events[0].kind == 'npc_spoke'
        assert store.pending_work('game') == []
        assert run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg)).events == ()


def test_due_work_commits_are_in_first_query_events():
    cue = PendingWork('due', 'scenario_cue', 0, 10, None,
        {'event_kind': 'moved', 'summary': '', 'effect_path': ['actors', 'dockhand', 'location'],
         'effect_value': 'harbor_square'})
    pkg, store = package(initial_work=(cue,)), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    engine = TurnEngine(store, pkg, ForbiddenModel())
    first = run(engine, request(pkg))
    assert 'due' in first.processed_work_ids
    assert any(event.event_id == 'due:occurred' for event in first.events)
    assert run(engine, request(pkg)).events == ()


def test_pending_free_generation_marker_rejected_before_query_writes():
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    recovery = GeneratedSceneRecovery(store, 'offline:v1', True)
    engine = TurnEngine(store, pkg, ForbiddenModel(), generated_scene_recovery=recovery)
    engine.session._claim_generation(store.load('game'), 'query-1', text_hash('uncommitted'), None)
    before, marker = store.load('game'), store.generated_scene_generation_marker('game', 'query-1')
    with pytest.raises(ValueError, match='pending free generation'):
        run(engine, request(pkg))
    assert store.load('game') == before
    assert store.generated_scene_generation_marker('game', 'query-1') == marker
    assert not store.event_exists('game', 'query-1:input')


def test_committed_free_turn_id_cannot_be_used_for_query():
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    free_engine = TurnEngine(store, pkg, OfflineModel())
    asyncio.run(free_engine.run_turn(TurnInput('game', 'Hello', 'query-1', pkg.version)))
    before = store.load('game')
    with pytest.raises(ValueError):
        run(TurnEngine(store, pkg, ForbiddenModel()), request(pkg))
    assert store.load('game') == before


def test_failure_and_cancellation_reset_event_context(monkeypatch):
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    engine = TurnEngine(store, pkg, ForbiddenModel())
    commit = store.commit
    def fail(*args, **kwargs):
        raise RuntimeError('commit stopped')
    monkeypatch.setattr(store, 'commit', fail)
    async def sequence():
        with pytest.raises(RuntimeError, match='commit stopped'):
            await engine.run_fact_query(request(pkg, turn='failed'))
        assert engine.store.records.get() is None
        monkeypatch.setattr(store, 'commit', commit)
        async def cancel(_):
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await engine.run_fact_query(request(pkg, turn='cancelled'), progress=cancel)
        assert engine.store.records.get() is None
        result = await engine.run_fact_query(request(pkg, turn='next'))
        assert engine.store.records.get() is None
        return result
    next_result = asyncio.run(sequence())
    assert next_result.events and all(event.event_id.startswith('next:') for event in next_result.events)


@pytest.mark.parametrize('shared_engine', [False, True])
def test_concurrent_engines_and_games_capture_only_their_commits(shared_engine):
    pkg, store = package(), InMemoryGameStore()
    for game in ('a', 'b'):
        pkg.seed_game(store, game)
    engines = {game: TurnEngine(store, pkg, ForbiddenModel()) for game in ('a', 'b')}
    if shared_engine:
        engines['b'] = engines['a']

    async def both():
        arrived = set()
        timed_out = set()
        release = asyncio.Event()

        async def query(game):
            async def progress(event):
                if event == {'type': 'stage', 'stage': 'committing'}:
                    arrived.add(game)
                    if len(arrived) == 2:
                        release.set()
                    try:
                        await asyncio.wait_for(release.wait(), timeout=2)
                    except TimeoutError:
                        timed_out.add(game)

            return await engines[game].run_fact_query(FactQueryInput(
                game, FactQuery('weather.confirmed', 'dockhand'), 'query-' + game, pkg.version),
                progress=progress)

        results = await asyncio.gather(query('a'), query('b'))
        assert arrived == {'a', 'b'}
        assert timed_out == set(), 'both queries must reach progress before either commits'
        return results

    a, b = asyncio.run(both())
    assert a.events and b.events
    assert all(event.event_id.startswith('query-a:') for event in a.events)
    assert all(event.event_id.startswith('query-b:') for event in b.events)
    assert {event.event_id for event in a.events}.isdisjoint(
        event.event_id for event in b.events)


def test_checkpoint_summarizer_is_skipped():
    pkg, store = package(), InMemoryGameStore()
    pkg.seed_game(store, 'game')
    calls = []
    async def forbidden_summary(_):
        calls.append(1)
        raise AssertionError('fact query refreshed summary')
    engine = TurnEngine(store, pkg, ForbiddenModel(), checkpoint_summarizer=forbidden_summary)
    result = run(engine, request(pkg))
    assert result.model_usage == () and calls == []
