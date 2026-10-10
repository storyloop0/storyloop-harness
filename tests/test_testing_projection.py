"""The diagnostic projection uses the real runtime without modifying the store."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from storyloop_harness import ScenarioPackage
from storyloop_harness.testing import InMemoryGameStore


def test_scene_request_is_detached_and_does_not_write():
    from storyloop_harness.testing import project_scene_request
    package = ScenarioPackage.load(Path(__file__).parents[1] / 'examples/freeform')
    store = InMemoryGameStore()
    package.seed_game(store, 'game', setup_state={
        'player_profile': {'name': 'Visitor', 'details': {'memento': ['shell']}}})
    snapshot = store.load('game')
    before = deepcopy(store._games)
    with patch.object(store, 'load', return_value=snapshot), \
            patch.object(store, 'commit', side_effect=AssertionError('must not commit')), \
            patch.object(store, 'create_game', side_effect=AssertionError('must not seed')), \
            patch.object(store, 'save_agent_context_checkpoint', side_effect=AssertionError('must not checkpoint')):
        request = project_scene_request(store, package, 'game', 'hello', context_window_tokens=7200)
        pristine = deepcopy(request)
        request['player_profile']['details']['memento'].append('changed')
        assert snapshot.data['player_profile']['details']['memento'] == ['shell']
        request['npc_contexts'][0]['role_card'] = 'changed'
        request['player_history'].append({'text': 'invented'})
        assert project_scene_request(store, package, 'game', 'hello', context_window_tokens=7200) == pristine
    assert store._games == before
    assert pristine['candidate_responders'] == ['dockhand']
    assert pristine['npc_contexts'][0]['role_card'] == package.role_cards['dockhand']
