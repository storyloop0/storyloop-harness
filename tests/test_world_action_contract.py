import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from scene_fixtures import EXAMPLE
from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.core.open_actions import parse_mutable_fields
from storyloop_harness.world.status_fields import parse_status_fields


class StructuredModel:
    def __init__(self, **changes):
        self.calls = 0
        self.payload = dict(prose='SUCCESS invented consequence', interaction='action',
                            action_id='break_shop_window', participants=['dockhand'],
                            replies=[{'actor_id': 'dockhand', 'speech': 'SUCCESS reply'}],
                            memories=[{'actor_id': 'dockhand', 'fact': 'SUCCESS memory'}],
                            action={'status': 'occurred', 'sensory': 'SUCCESS noise'},
                            status_changes=[{'id': 'pressure', 'delta': 1}])
        self.payload.update(changes)

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(metadata=self.payload)


def setup_world(**state_changes):
    base = ScenarioPackage.load(EXAMPLE)
    state = deepcopy(base.initial_state)
    state['stats'] = {'pressure': 3}
    state.update(state_changes)
    fields = parse_status_fields([{'id': 'pressure', 'label': 'Pressure',
        'path': ['stats', 'pressure'], 'bounds': {'min': 0, 'max': 10, 'max_delta': 2}}], state)
    package = replace(base, initial_state=state, initial_work=(), status_fields=fields)
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    return store, package


def run(store, package, model, turn='one', **kwargs):
    return asyncio.run(TurnEngine(store, package, model, **kwargs).run_turn(
        TurnInput('game', 'I break the window', turn, package.version)))


def test_public_known_action_status_and_restart_replay():
    store, package = setup_world()
    model = StructuredModel()
    result = run(store, package, model)
    assert result.snapshot.data['world']['shop_window']['broken'] is True
    assert result.snapshot.data['stats']['pressure'] == 4
    event = result.events[0]
    assert event.details['outcome'] == 'accepted'
    assert {effect.path for effect in event.effects} == {
        ('world', 'shop_window', 'broken'), ('stats', 'pressure')}
    replay = run(store, package, model)
    assert replay.snapshot == result.snapshot
    assert replay.narration == result.narration
    assert model.calls == 1


def test_known_action_uses_authored_result_when_model_prose_contradicts_state():
    store, package = setup_world()
    model = StructuredModel(prose='The window is untouched and remains intact.', replies=[], memories=[])
    result = run(store, package, model)
    assert result.snapshot.data['world']['shop_window']['broken'] is True
    assert result.narration == package.action_rules['break_shop_window'].sensory
    assert 'untouched' not in store.event_details('game', 'one:action')['single_call']['prose']


@pytest.mark.parametrize('value', [{'items': [1, None]}, ['one', {'two': True}], None, 1.5])
def test_public_authored_assignment_and_restart_replay(value):
    from storyloop_harness.core.actions import parse_action_rules

    store, package = setup_world()
    rule = package.action_rules['break_shop_window']
    rules = parse_action_rules([{
        'id': rule.action_id, 'label': rule.label, 'location': rule.location,
        'when': {'path': list(rule.when_path), 'value': rule.when_value},
        'effects': [{'path': ['world', 'shop_window', 'broken'], 'value': value}],
        'sensory': rule.sensory,
    }], package.initial_state)
    package = replace(package, action_rules=rules)
    model = StructuredModel(replies=[], memories=[], status_changes=[], action={
        'status': 'occurred', 'effects': [
            {'path': ['world', 'shop_window', 'broken'], 'value': True}]})
    result = run(store, package, model)
    assert result.snapshot.data['world']['shop_window']['broken'] == value
    assert type(result.snapshot.data['world']['shop_window']['broken']) is type(value)
    assert result.events[0].effects == rules[rule.action_id].effects
    assert result.narration == rule.sensory
    saved = store.event_details('game', 'one:action')['single_call']
    assert saved['action']['effects'] == []
    replay = run(store, package, model)
    assert replay.snapshot == result.snapshot
    assert replay.narration == result.narration
    assert model.calls == 1


@pytest.mark.parametrize('reason', ['blocked', 'repeated', 'location', 'unknown'])
def test_public_rejected_action_discards_success_and_sidecars(reason):
    store, package = setup_world()
    model = StructuredModel()
    if reason == 'blocked':
        model.payload['action']['status'] = 'blocked'
    elif reason == 'repeated':
        run(store, package, model, 'prior')
    elif reason == 'location':
        state = deepcopy(package.initial_state)
        state['actors']['player']['location'] = 'elsewhere'
        package = replace(package, initial_state=state)
        store = InMemoryGameStore()
        package.seed_game(store, 'game')
    else:
        model.payload['action_id'] = 'invented'
    before = store.load('game')
    result = run(store, package, model)
    assert 'SUCCESS' not in result.narration
    assert result.events[0].details['outcome'] == 'rejected'
    assert result.events[0].effects == ()
    assert result.snapshot.data == before.data
    assert result.processed_work_ids == ()
    saved = store.event_details('game', 'one:action')['single_call']
    for key in ('replies', 'memories', 'status_changes', 'milestones_fulfilled', 'encounters', 'options'):
        assert saved[key] == []


@pytest.mark.parametrize('effect,accepted', [
    ({'path': ['world', 'shop_window', 'broken'], 'value': True}, True),
    ({'path': ['world', 'shop_window', 'broken'], 'value': 1}, False),
    ({'path': ['world', 'market_phase'], 'value': 'open'}, False),
    ({'path': ['world', 'shop_window', 'broken'], 'value': False}, False),
    ({'path': ['stats', 'pressure'], 'value': 4}, False),
])
def test_public_open_effect_allowlist_transition_type_and_collision(effect, accepted):
    store, package = setup_world()
    fields = parse_mutable_fields([
        {'path': ['world', 'shop_window', 'broken'], 'values': [False, True],
         'transitions': [{'from': False, 'to': [True]}]},
        {'path': ['stats', 'pressure'], 'values': [3, 4]}], package.initial_state)
    package = replace(package, mutable_fields=fields)
    model = StructuredModel(action_id=None, action={'status': 'occurred', 'effects': [effect]})
    result = run(store, package, model)
    assert result.events[0].details['outcome'] == ('accepted' if accepted else 'rejected')
    assert result.snapshot.data['world']['shop_window']['broken'] is accepted
    assert result.snapshot.data['stats']['pressure'] == (4 if accepted else 3)
    if not accepted:
        assert 'SUCCESS' not in result.narration


def test_projection_action_declarations_provenance_and_state_scope():
    store, package = setup_world()
    package = replace(package, mutable_fields=parse_mutable_fields([
        {'path': ['world', 'shop_window', 'broken'], 'values': [False, True]}], package.initial_state))
    projection = TurnEngine(store, package, StructuredModel()).project_context(
        TurnInput('game', 'break window', 'inspect', package.version))
    assert projection.request['actions'][0]['id'] == 'break_shop_window'
    assert projection.request['actions'][0]['available'] is True
    assert projection.request['mutable_state'][0]['current'] is False
    assert 'world' not in projection.request
    assert any('action_rules' in str(item) for item in projection.report.decisions)


def test_remaining_ready_work_is_public_and_durable():
    from scene_fixtures import source_package
    package = source_package()
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    model = StructuredModel(interaction='speech', action_id=None, status_changes=[],
        participants=['dockhand', 'vendor'],
        replies=[{'actor_id': who, 'speech': 'hello'} for who in ['dockhand', 'vendor']],
        options=[{'label': str(i), 'input': str(i)} for i in range(3)])
    result = run(store, package, model, max_steps=1)
    assert len(result.remaining_work_ids) == 1
    assert result.remaining_work_ids == tuple(work.work_id for work in store.ready_work('game', result.snapshot.tick))


def test_effect_scalar_must_keep_exact_model_type():
    store, package = setup_world()
    package = replace(package, mutable_fields=parse_mutable_fields([
        {'path': ['stats', 'pressure'], 'values': [3, 4]}], package.initial_state), status_fields=())
    model = StructuredModel(action_id=None, status_changes=[],
        action={'status': 'occurred', 'effects': [{'path': ['stats', 'pressure'], 'value': 4.0}]})
    before = store.load('game')
    with pytest.raises(ValueError):
        run(store, package, model)
    assert store.load('game') == before


@pytest.mark.parametrize('mode', ['hidden', 'ineligible'])
def test_public_status_rejects_hidden_or_conditional_ineligible_field(mode):
    store, package = setup_world()
    field = package.status_fields[0]
    field = replace(field, visible=False) if mode == 'hidden' else replace(
        field, when_path=('world', 'shop_window', 'broken'), when_equals=True)
    package = replace(package, status_fields=(field,))
    before = store.load('game')
    with pytest.raises(ValueError, match='automatically updated'):
        run(store, package, StructuredModel(interaction='speech', action_id=None))
    assert store.load('game') == before


def test_inspection_id_and_action_metadata_survive_storage():
    from storyloop_harness.agents.scene_schema import NarrativeTurn
    plan = NarrativeTurn(prose='look', interaction='inspect', entry_id='notice').for_storage()
    assert plan.decision.entry_id == 'notice'
    action = NarrativeTurn(prose='act', interaction='action', action_id='break_shop_window',
        action={'status': 'occurred', 'player_result': 'done', 'sensory': 'noise'}).for_storage()
    assert action.decision.action_id == 'break_shop_window'
    assert action.action.player_result == 'done'
