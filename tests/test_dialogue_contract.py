"""Single-source content, persistence order, and bounded repetition checks."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scene_fixtures import FakeGenerator, source_package
from storyloop_harness.agents.scene_schema import NarrativeTurn, SceneTurn
from storyloop_harness.agents.scene_turn import SingleSceneGenerator
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.core.generated_scene import GeneratedSceneRecovery, GeneratedSceneUncertain
from storyloop_harness.runtime.presentation import segment_for_observation
from storyloop_harness.runtime.single_call import SingleCallGameSession
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import InMemoryGameStore


def narration(text):
    return dict(kind='narration', text=text)


def dialogue(actor, text):
    return dict(kind='dialogue', actor_id=actor, text=text)


@pytest.mark.parametrize('payload', [
    {'prose': 'He says: The tide is still unknown.', 'replies': [
        {'actor_id': 'dockhand', 'speech': 'The tide is still unknown.'}]},
    {'beats': [narration('Valid')], 'prose': 'old'},
    {'beats': [narration('Valid')], 'replies': []},
    {'beats': []}, {'beats': [narration('  \\n  ')]},
    {'beats': [dict(kind='other', text='x')]},
    {'beats': [dict(kind='dialogue', text='x')]},
    {'beats': [dialogue(' ', 'x')]},
    {'beats': [dict(kind='narration', text='x', actor_id='dockhand')]},
    {'beats': [dict(kind='narration', text='x', speech='copy')]},
    {'beats': [narration(123)]},
])
def test_invalid_generated_content_is_rejected(payload):
    with pytest.raises(ValueError):
        NarrativeTurn.model_validate(payload)


@pytest.mark.parametrize('beats', [[narration('Only setting.')],
    [dialogue('dockhand', 'Only speech.')],
    [narration('Start.'), dialogue('dockhand', 'First.'), narration('Pause.'),
     dialogue('dockhand', 'Second.'), dialogue('vendor', 'First.')]])
def test_content_round_trips_in_order_without_a_second_source(beats):
    turn = NarrativeTurn.model_validate({'beats': beats}).for_storage()
    assert turn.model_dump()['beats'] == beats
    assert not {'prose', 'replies'} & turn.model_dump().keys()
    assert SceneTurn.model_validate(turn.model_dump()) == turn


def test_bounded_full_answer_repetition_check_and_short_overlap():
    speech = 'The east pier has not been checked.'
    with pytest.raises(ValueError, match='repeat'):
        NarrativeTurn(beats=[narration(f'He says, “{speech}”'), dialogue('dockhand', speech)])
    with pytest.raises(ValueError, match='repeat'):
        NarrativeTurn(beats=[narration('他说：“今夜的风向还没有确认。”'),
                             dialogue('dockhand', '今夜的风向还没有确认。')])
    NarrativeTurn(beats=[narration('The pier is quiet.'), dialogue('dockhand', 'The pier?')])
    NarrativeTurn(beats=[narration('He says yes.'), dialogue('dockhand', 'yes')])
    NarrativeTurn(beats=[dialogue('dockhand', speech), dialogue('vendor', speech)])


def session(store, package, generator, **kwargs):
    return SingleCallGameSession(store, package, generator,
        SceneContextProjector(store, package, max_responders=4), **kwargs)


@pytest.mark.parametrize('mode', ['interactive', 'novel'])
def test_order_repeated_speakers_sqlite_reopen_and_partial_work(mode, tmp_path):
    package = replace(source_package(), story_blueprint=None, presentation_mode=mode)
    beats = [narration('Start.'), dialogue('dockhand', 'First answer.'),
             narration('Pause.'), dialogue('vendor', 'First answer.'),
             dialogue('dockhand', 'Second answer.'), narration('End.')]
    generator = FakeGenerator(dict(beats=beats, decision=dict(intent='speech',
        target_ids=['dockhand', 'vendor'], audience='room')))
    path = tmp_path / 'scene.db'
    with SQLiteGameStore(path) as store:
        package.seed_game(store, 'game')
        first = asyncio.run(session(store, package, generator, max_steps=1).run_turn(
            'game', 'Hello', 'turn'))
        assert first.narration.index('Start.') < first.narration.index('First answer.')
        assert first.narration.index('Pause.') < first.narration.rindex('First answer.')
        assert first.narration.index('Second answer.') < first.narration.index('End.')
        assert first.narration.count('First answer.') == 2
        visible = tuple(store.observations_for('game', 'player'))
        assert '\n\n'.join(segment_for_observation(store, 'game', obs).body_text
                          for obs in visible) == first.narration
    with SQLiteGameStore(path) as store:
        replay = asyncio.run(session(store, package, generator).run_turn('game', 'Hello', 'turn'))
        assert replay.narration == first.narration
        assert tuple(store.observations_for('game', 'player')) == visible
        assert generator.calls == 1
        own = [e for e in store.agent_context_entries('game', 'dockhand')
               if e.entry_id.startswith('turn:input:reply:dockhand')]
        assert len(own) == 2
        assert 'First answer.' in own[0].content and 'Second answer.' in own[1].content


def test_unprojected_dialogue_rejected_before_player_event():
    package = replace(source_package(), story_blueprint=None)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    generator = FakeGenerator(dict(beats=[dialogue('unknown', 'Secret.')],
                                  decision=dict(intent='speech', target_ids=['unknown'])))
    with pytest.raises(ValueError, match='actor|authorized'):
        asyncio.run(session(store, package, generator).run_turn('game', 'Hello', 'turn'))
    assert store.player_inputs_for('game') == []


def test_failed_generation_preserves_uncertainty_marker_without_snapshot(tmp_path):
    package = replace(source_package(), story_blueprint=None)
    class BadModel:
        calls = 0
        async def __call__(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(metadata={'prose': 'Old duplicate.', 'replies': []})
    model = BadModel()
    with SQLiteGameStore(tmp_path / 'invalid.db') as store:
        package.seed_game(store, 'game')
        runtime = session(store, package, SingleSceneGenerator(model, package),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'test', protect_uncertain_generation=True))
        with pytest.raises(ValueError):
            asyncio.run(runtime.run_turn('game', 'Hello', 'turn'))
        assert store.generated_scene_snapshot('game', 'turn') is None
        assert store.generated_scene_generation_marker('game', 'turn') is not None
        assert store.player_inputs_for('game') == []
        with pytest.raises(GeneratedSceneUncertain):
            asyncio.run(runtime.run_turn('game', 'Hello', 'turn'))
        assert model.calls == 1


@pytest.mark.parametrize('payload', [
    {'prose': 'obsolete'}, {'beats': []},
    {'beats': [narration('He says: The east pier has not been checked.'),
               dialogue('dockhand', 'The east pier has not been checked.')]},
])
def test_actual_sdk_content_rejection_uses_one_send_and_retains_attempt_usage(payload):
    import httpx2
    from model_helpers import create_test_model
    from test_request_boundary import completion_payload
    from storyloop_harness.usage import collect_attempts
    async def run():
        sent = []
        def respond(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion_payload(json.dumps(payload)))
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        model = create_test_model(http_client=client)
        model.max_retries = 0
        try:
            package = replace(source_package(), story_blueprint=None)
            store = InMemoryGameStore()
            package.seed_game(store, 'game')
            runtime = session(store, package, SingleSceneGenerator(model, package),
                generated_scene_recovery=GeneratedSceneRecovery(
                    store, 'offline:test', protect_uncertain_generation=True))
            with collect_attempts() as attempts:
                with pytest.raises(Exception):
                    await runtime.run_turn('game', 'Hello', 'turn')
            assert store.generated_scene_snapshot('game', 'turn') is None
            assert store.generated_scene_generation_marker('game', 'turn') is not None
            assert store.player_inputs_for('game') == []
            with pytest.raises(GeneratedSceneUncertain):
                await runtime.run_turn('game', 'Hello', 'turn')
            assert len(sent) == 1
            assert len(attempts.records) == 1
            assert attempts.records[0].outcome == 'failed'
            assert attempts.records[0].usage_state == 'reported'
            assert attempts.records[0].usage.input_tokens == 1
            assert attempts.records[0].usage.output_tokens == 1
            schema = sent[0]['tools'][0]['function']['parameters']
            assert 'beats' in schema['required']
            assert not {'prose', 'replies'} & schema['properties'].keys()
            assert 'sidecar' not in json.dumps(sent[0]['tools'])
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_source_dialogue_introduction_accepts_identity_from_same_beat():
    package = source_package()
    state = dict(package.initial_state, player_knowledge={'seen_actor_ids': [], 'named_actor_ids': []})
    package = replace(package, initial_state=state)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    plan = dict(beats=[dialogue('dockhand', '我叫Dockhand。')],
        decision=dict(intent='speech', target_ids=['dockhand']),
        encounters=[dict(actor_id='dockhand', evidence='我叫Dockhand。', name_learned=True)])
    result = asyncio.run(session(store, package, FakeGenerator(plan)).run_turn('game', 'Hello', 'turn'))
    assert result.segments[0].speaker_name == 'Dockhand'
    assert result.snapshot.data['player_knowledge']['named_actor_ids'] == ['dockhand']
    assert result.narration.count('我叫Dockhand。') == 1


@pytest.mark.parametrize('mode', ['interactive', 'novel'])
def test_unknown_identity_label_and_private_speech(mode):
    package = source_package()
    state = dict(package.initial_state, player_knowledge={'seen_actor_ids': [], 'named_actor_ids': []})
    package = replace(package, initial_state=state, presentation_mode=mode)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    plan = dict(beats=[dialogue('dockhand', 'Only for you.')], decision=dict(
        intent='speech', target_ids=['dockhand'], channel='private_message'),
        memories=[dict(actor_id='dockhand', fact='Promised a private explanation.')])
    result = asyncio.run(session(store, package, FakeGenerator(plan)).run_turn('game', 'Private', 'turn'))
    assert '一位嘉宾' in result.narration and 'Dockhand' not in result.narration
    assert not any(e.channel == 'overheard' for e in store.observations_for('game', 'vendor'))
    assert any('Only for you.' in e.content for e in store.agent_context_entries('game', 'dockhand'))
    assert any(e.channel == 'shared_experience' for e in store.observations_for('game', 'dockhand'))


def test_custom_generator_cannot_bypass_validation_with_model_construct():
    package = replace(source_package(), story_blueprint=None)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    class Generator:
        async def generate(self, *args):
            good = NarrativeTurn(beats=[narration('A scene.')]).for_storage()
            return good.model_copy(update={'beats': []})
    with pytest.raises(ValueError):
        asyncio.run(session(store, package, Generator()).run_turn('game', 'Hello', 'turn'))
    assert store.player_inputs_for('game') == []


def test_legacy_committed_snapshot_replays_but_uncommitted_snapshot_is_rejected(tmp_path):
    from storyloop_harness.agents.scene_schema import LegacySceneTurn
    from storyloop_harness.core.contracts import Observation, WorldEvent
    from storyloop_harness.core.generated_scene import (
        GeneratedSceneSnapshot, canonical_json, content_hash, text_hash, GeneratedSceneMismatch)
    from test_generated_scene_snapshot import make_record
    package = replace(source_package(), story_blueprint=None)
    old = LegacySceneTurn(decision={'intent': 'speech'}, prose='Historical visible scene.')
    generator = FakeGenerator({})
    with SQLiteGameStore(tmp_path / 'legacy.db') as store:
        package.seed_game(store, 'game')
        runtime = session(store, package, generator,
            generated_scene_recovery=GeneratedSceneRecovery(store, 'test'))
        before = store.load('game')
        body = make_record(package).metadata()
        body.update(runtime._bindings(before, text_hash('Hello'), None))
        body.update(scene=old.model_dump(), generation_contract_version=1,
                    system_prompt_hash='d'*64, output_schema_hash='e'*64)
        body['snapshot_id'] = content_hash({k: v for k, v in body.items() if k != 'snapshot_id'})
        encoded = canonical_json(body)
        record = GeneratedSceneSnapshot.from_storage(encoded, text_hash(encoded))
        store.save_generated_scene_snapshot(record)
        with pytest.raises(GeneratedSceneMismatch):
            asyncio.run(runtime.run_turn('game', 'Hello', 'turn'))
        details = dict(text='Hello', single_call=old.model_dump(),
                       generated_scene_snapshot_id=record.snapshot_id,
                       generated_scene_snapshot_checksum=record.checksum)
        store.commit('game', 0, WorldEvent('turn:input', 'player_input', 'player', None, 1, (), details),
            (Observation('turn:input:scene', 'turn:input', 'player', 'scene', old.prose, 1),), ())
        replay = asyncio.run(runtime.run_turn('game', 'Hello', 'turn'))
        assert replay.narration == old.prose
        assert generator.calls == 0
