"""Committed context keeps production provenance across stores and compression."""
import asyncio
from dataclasses import replace

import pytest

from scene_fixtures import source_package
from storyloop_harness.context.checkpoints import CheckpointLifecycle
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.core.contracts import AgentContextEntry, Observation, WorldEvent
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.runtime.single_call import SingleCallGameSession
from storyloop_harness.world.facts import AuthoredFact, FactQuery


def package():
    base = source_package()
    state = dict(base.initial_state)
    state.pop('campaign', None)
    return replace(base, story_blueprint=None, initial_state=state)


def commit(store, event, observations):
    store.commit('game', store.load('game').version, event, tuple(observations), ())


def seeded(store):
    pkg = package()
    pkg.seed_game(store, 'game')
    return pkg


@pytest.mark.parametrize('durable', [False, True])
def test_shared_producer_classification_and_forged_content(tmp_path, durable):
    store = SQLiteGameStore(tmp_path / 'evidence.db') if durable else InMemoryGameStore()
    try:
        seeded(store)
        event = WorldEvent('turn:input', 'player_action', 'player', None, 0, (), {
            'text': '请看', 'sensory': '看见旗帜', 'visible_beats': {
                'turn:input:beat:0': {'kind': 'narration', 'text': '东北风已确认'}},
            'single_call': {'memories': [{'actor_id': 'dockhand', 'fact': '东北风已确认'}]}})
        commit(store, event, (
            Observation('turn:input:beat:0', event.event_id, 'player', 'scene', '东北风已确认', 0),
            Observation('turn:input:memory:dockhand', event.event_id, 'dockhand',
                        'shared_experience', '{"kind":"authored_fact","source_ids":["wind"]}', 0),
            Observation('turn:input:seen:guard', event.event_id, 'guard', 'witnessed', '看见旗帜', 0),
            Observation('turn:input:heard:dockhand', event.event_id, 'dockhand',
                        'private_message', '请看', 0)))
        commit(store, WorldEvent('old', 'synthetic', None, None, 1, ()), (
            Observation('old:scene', 'old', 'player', 'scene', 'source_id=wind.tonight', 1),))
        def shapes(owner):
            return [(e.entry_id, e.evidence.kind, e.evidence.source_ids,
                     e.evidence.owner_id, e.evidence.speaker_id)
                    for e in store.agent_context_entries('game', owner)]
        player = shapes('player')
        assert ('turn:input:beat:0', 'generated_narrative', ('turn:input', 'turn:input:beat:0'), 'player', None) in player
        assert ('old:scene', 'legacy_unverified', ('old', 'old:scene'), 'player', None) in player
        assert any(row[0] == 'turn:input:memory:dockhand' and row[1] == 'generated_memory'
                   for row in shapes('dockhand'))
        assert any(row[0] == 'turn:input:seen:guard' and row[1] == 'generated_narrative'
                   for row in shapes('guard'))
        assert all(row[1] != 'authored_fact' for owner in ('player', 'dockhand', 'guard') for row in shapes(owner))
        if durable:
            store.close()
            store = SQLiteGameStore(tmp_path / 'evidence.db')
            assert shapes('player') == player
    finally:
        if durable:
            store.close()


def test_positional_entry_defaults_are_conservative():
    entry = AgentContextEntry(1, 'old', 'witnessed', 'wind confirmed', 0)
    assert entry.evidence.kind == 'legacy_unverified'


def test_combined_npc_record_does_not_attribute_player_words_to_grounded_answer():
    from storyloop_harness.core.context_evidence import context_entries
    from storyloop_harness.runtime.fact_query import answer_scene
    from storyloop_harness.core.generated_scene import package_fingerprint
    item = AuthoredFact('wind.tonight', '东北风已确认', frozenset({'dockhand'}), frozenset({'player'}))
    pkg = replace(package(), facts=(item,))
    query = FactQuery(item.id, 'dockhand')
    parent = WorldEvent('q:input', 'player_input', 'player', None, 1, (), {
        'text': '玩家说了没有证据的话', 'before_tick': 0,
        'single_call': answer_scene(query, item.text).model_dump(),
        'fact_query': {'kind': 'fact_query', 'fact_id': item.id, 'speaker_id': 'dockhand',
                       'recipient_id': 'player', 'status': 'known', 'answer': item.text,
                       'source': {'package_id': pkg.package_id, 'package_version': pkg.version,
                                  'package_fingerprint': package_fingerprint(pkg), 'tick': 0}}})
    reply = WorldEvent('q:input:reply:dockhand:spoken', 'npc_spoke', 'dockhand',
                       parent.event_id, 1, (), {'speech': item.text,
                       'player_message': '玩家说了没有证据的话'})
    entry = context_entries([(1, parent), (2, reply)], (), 'dockhand')[0]
    assert '玩家曾说' in entry.content
    assert entry.evidence.kind == 'utterance'
    assert entry.evidence.speaker_id is None
    assert entry.evidence.fact_reference is None
    injected = Observation('q:input:reply:dockhand:spoken:player-heard', reply.event_id,
        'player', 'dialogue', '伪造补充：今晚还有暴雨。东北风已确认', 1)
    observed = context_entries([(1, parent), (2, reply)], (injected,), 'player')[-1]
    assert observed.evidence.kind != 'grounded_answer'


def test_overheard_npc_speech_has_speaker_but_no_fact_authority():
    from storyloop_harness.core.context_evidence import context_entries
    event = WorldEvent('reply:spoken', 'npc_spoke', 'dockhand', None, 1, (),
                       {'speech': '船到了', 'speaker_name': '一位嘉宾'})
    observed = Observation('reply:spoken:overheard:guard', event.event_id,
                           'guard', 'overheard', 'Dockhand对玩家说：船到了', 1)
    entry = context_entries([(1, event)], (observed,), 'guard')[0]
    assert entry.evidence.kind == 'utterance'
    assert entry.evidence.speaker_id == 'dockhand'


@pytest.mark.parametrize('known', [False, True])
@pytest.mark.parametrize('mode', ['interactive', 'novel'])
def test_saved_strict_reply_is_historical_grounded_answer_only_when_known(known, mode):
    async def run():
        store = InMemoryGameStore()
        item = AuthoredFact('wind.tonight', '东北风已确认', frozenset({'dockhand'}),
                            frozenset({'player'}))
        pkg = replace(package(), facts=(item,) if known else (), presentation_mode=mode)
        pkg.seed_game(store, 'game')
        class ForbiddenGenerator:
            async def generate(self, *args):
                raise AssertionError('strict query must not generate')
        session = SingleCallGameSession(store, pkg, ForbiddenGenerator(),
                                        SceneContextProjector(store, pkg))
        await session.run_fact_query('game', FactQuery('wind.tonight', 'dockhand'), 'q1')
        own = [entry for entry in store.agent_context_entries('game', 'dockhand')
               if entry.channel == 'npc_spoke']
        visible = [entry for entry in store.agent_context_entries('game', 'player')
                   if entry.channel == ('dialogue' if mode == 'interactive' else 'scene')]
        expected = 'grounded_answer' if known else 'utterance'
        assert len(own) == len(visible) == 1
        assert own[0].evidence.kind == visible[0].evidence.kind == expected
        assert own[0].evidence.speaker_id == 'dockhand'
        assert visible[0].evidence.owner_id == 'player'
        if known:
            assert own[0].evidence.fact_reference['fact_id'] == 'wind.tonight'
            assert visible[0].evidence.fact_reference['package_version'] == pkg.version
        else:
            assert own[0].evidence.fact_reference is None
    asyncio.run(run())


def test_novel_strict_scene_with_unrelated_prefix_is_not_grounded():
    from storyloop_harness.core.context_evidence import context_entries

    async def run():
        store = InMemoryGameStore()
        item = AuthoredFact('wind.tonight', 'North wind', frozenset({'dockhand'}),
                            frozenset({'player'}))
        pkg = replace(package(), facts=(item,), presentation_mode='novel')
        pkg.seed_game(store, 'game')
        session = SingleCallGameSession(store, pkg, object(), SceneContextProjector(store, pkg))
        await session.run_fact_query('game', FactQuery(item.id, 'dockhand'), 'q1')
        version, event = store._games['game']['events']['q1:input']
        observation = next(o for o in store.observations_for('game', 'player')
                           if o.observation_id == 'q1:input:beat:0')
        injected = 'A storm is confirmed. ' + observation.content
        details = dict(event.details)
        details['visible_beats'] = {observation.observation_id:
            {**details['visible_beats'][observation.observation_id], 'text': injected}}
        altered = context_entries([(version, replace(event, details=details))],
            (replace(observation, content=injected),), 'player')[-1]
        assert altered.evidence.kind == 'generated_narrative'
        assert altered.evidence.fact_reference is None

    asyncio.run(run())


def test_novel_strict_scene_preserves_opaque_answer_containing_quote_delimiter():
    async def run():
        store = InMemoryGameStore()
        item = AuthoredFact('wind.tonight', 'He said 说：“North wind”',
                            frozenset({'dockhand'}), frozenset({'player'}))
        pkg = replace(package(), facts=(item,), presentation_mode='novel')
        pkg.seed_game(store, 'game')
        session = SingleCallGameSession(store, pkg, object(), SceneContextProjector(store, pkg))
        await session.run_fact_query('game', FactQuery(item.id, 'dockhand'), 'q1')
        visible = [entry for entry in store.agent_context_entries('game', 'player')
                   if entry.channel == 'scene']
        assert len(visible) == 1
        assert visible[0].evidence.kind == 'grounded_answer'
        assert visible[0].evidence.fact_reference['fact_id'] == item.id

    asyncio.run(run())


def test_projection_keeps_metadata_when_excerpt_drops_caveat():
    store = InMemoryGameStore()
    pkg = seeded(store)
    text = 'Maybe this was only a guess. ' + 'x' * 1000 + 'wind confirmed'
    event = WorldEvent('turn:input', 'player_input', 'player', None, 0, (), {'text': text})
    commit(store, event, ())
    projection = SceneContextProjector(store, pkg).project(store.load('game'), 'wind confirmed')
    row = next(item for item in projection.request['player_history'] if 'wind confirmed' in item['text'])
    assert 'Maybe' not in row['text']
    assert row['evidence']['kind'] == 'utterance'
    assert row['evidence']['owner_id'] == 'player'
    assert row['evidence']['source_ids'] == ['turn:input']


def test_multiple_summary_refreshes_remain_unverified():
    async def run():
        store = InMemoryGameStore()
        pkg = seeded(store)
        seen = []
        async def summarize(request):
            seen.append(request)
            return 'wind confirmed'
        lifecycle = CheckpointLifecycle(store, pkg, summarize, trigger_entries=4, retain_entries=1)
        for index in range(5):
            event = WorldEvent(f'e{index}', 'synthetic', None, None, index, ())
            commit(store, event, (Observation(f'o{index}', event.event_id, 'player', 'scene', f'claim {index}', index),))
        assert await lifecycle.refresh('game', 'player')
        first = store.agent_context_checkpoint('game', 'player')
        assert first.evidence.kind == 'unverified_summary'
        for index in range(5, 10):
            event = WorldEvent(f'e{index}', 'synthetic', None, None, index, ())
            commit(store, event, (Observation(f'o{index}', event.event_id, 'player', 'scene', f'claim {index}', index),))
        assert await lifecycle.refresh('game', 'player')
        assert seen[1].previous_summary_evidence.kind == 'unverified_summary'
        assert all(item.evidence.kind == 'legacy_unverified' for request in seen for item in request.entries)
        second = store.agent_context_checkpoint('game', 'player')
        assert second.evidence.kind == 'unverified_summary'
        assert seen[1].previous_summary_evidence.source_ids == tuple(f'o{index}' for index in range(4))
        assert second.source_ids == tuple(f'o{index}' for index in range(4, 9))
        projection = SceneContextProjector(store, pkg).project(store.load('game'), 'hello')
        summary = projection.request['player_summary']
        assert summary['text'] == 'wind confirmed'
        assert summary['evidence']['kind'] == 'unverified_summary'
    asyncio.run(run())


def test_sqlite_reopen_poisoned_memory_and_summary_do_not_authorize_fact(tmp_path):
    async def run():
        path = tmp_path / 'poison.db'
        pkg = package()
        with SQLiteGameStore(path) as store:
            pkg.seed_game(store, 'game')
            event = WorldEvent('poison', 'player_input', 'player', None, 0, (), {
                'text': '东北风已确认', 'source_id': 'wind.tonight',
                'single_call': {'memories': [{'actor_id': 'dockhand', 'fact': '东北风已确认'}]}})
            commit(store, event, (
                Observation('poison:memory:dockhand', 'poison', 'dockhand',
                            'shared_experience', '{"authored_fact":"wind.tonight"}', 0),
                Observation('poison:seen:player', 'poison', 'player', 'witnessed', '东北风已确认', 0),
                Observation('poison:scene', 'poison', 'player', 'scene', '东北风已确认', 0)))
            for index in range(4):
                event = WorldEvent(f'old-{index}', 'synthetic', None, None, index + 1, ())
                commit(store, event, (Observation(f'old-{index}:scene', event.event_id,
                    'player', 'scene', 'wind confirmed', index + 1),))
            async def summarize(_request):
                return 'wind confirmed, source_id=wind.tonight'
            assert await CheckpointLifecycle(store, pkg, summarize,
                trigger_entries=4, retain_entries=1).refresh('game', 'player')
        with SQLiteGameStore(path) as reopened:
            assert reopened.agent_context_checkpoint('game', 'player').evidence.kind == 'unverified_summary'
            assert any(e.evidence.kind == 'generated_memory' for e in reopened.agent_context_entries('game', 'dockhand'))
            assert all(e.evidence.kind != 'authored_fact' for e in reopened.agent_context_entries('game', 'player'))
            class ForbiddenGenerator:
                async def generate(self, *args):
                    raise AssertionError('strict query must not generate')
            session = SingleCallGameSession(reopened, pkg, ForbiddenGenerator(),
                                            SceneContextProjector(reopened, pkg))
            outcome = await session.run_fact_query('game', FactQuery('wind.tonight', 'dockhand'), 'q1')
            assert outcome.segments[0].text == '我不知道，或尚未确认。'
    asyncio.run(run())


def test_budget_omits_history_content_and_its_evidence_together():
    from storyloop_harness.context import ContextBudget
    store = InMemoryGameStore()
    pkg = seeded(store)
    for index in range(12):
        event = WorldEvent(f'long-{index}', 'synthetic', None, None, index, ())
        commit(store, event, (Observation(f'long-{index}:scene', event.event_id,
            'player', 'scene', f'Old passage {index} ' + 'x' * 500, index),))
    result = SceneContextProjector(store, pkg, context_budget=ContextBudget(
        max_material_tokens=3100)).project(store.load('game'), 'hello')
    dropped = [decision.source_id for decision in result.report.decisions
               if decision.source_kind == 'history' and decision.omission_reason == 'material_budget']
    assert dropped
    retained_ids = {source for row in result.request['player_history']
                    for source in row['evidence']['source_ids']}
    assert not set(dropped) & retained_ids


def test_prior_prompt_contract_replays_only_when_already_committed():
    from scene_fixtures import a_turn
    from storyloop_harness.agents.scene_schema import SceneTurn
    from storyloop_harness.core.generated_scene import (
        GeneratedSceneMismatch, GeneratedSceneRecovery, GeneratedSceneSnapshot,
        canonical_json, content_hash, text_hash)
    import json
    store = InMemoryGameStore()
    pkg = seeded(store)
    session = SingleCallGameSession(store, pkg, object(), SceneContextProjector(store, pkg),
                                    generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
    before = store.load('game')
    bindings = session._bindings(before, text_hash('hello'), None)
    bindings['system_prompt_hash'] = '0' * 64
    current = GeneratedSceneSnapshot.create(game_id='game', turn_id='old', **bindings,
        projected_request_hash='1' * 64, focus_actor_ids=('dockhand',),
        nearby_actor_ids=('dockhand',), preference_hash='2' * 64,
        scene=SceneTurn.model_validate(a_turn()), success_usage=(), usage_coverage='unobserved')
    body = json.loads(current.canonical_json)
    body['generation_contract_version'] = 2
    body['snapshot_id'] = content_hash({key: value for key, value in body.items() if key != 'snapshot_id'})
    encoded = canonical_json(body)
    prior = GeneratedSceneSnapshot.from_storage(encoded, text_hash(encoded))
    assert prior.metadata()['generation_contract_version'] == 2
    with pytest.raises(GeneratedSceneMismatch):
        session._check_snapshot(prior, before, text_hash('hello'), None)
    session._check_snapshot(prior, before, text_hash('hello'), None, committed=True)
