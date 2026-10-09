import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.advanced import PendingWork, WorldEvent
from storyloop_harness.testing import InMemoryGameStore, OfflineModel


class CountingModel(OfflineModel):
    def __init__(self):
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return await super().__call__(*args, **kwargs)


class RecordingStore(InMemoryGameStore):
    def __init__(self):
        super().__init__()
        self.commit_calls = 0

    def commit(self, *args, **kwargs):
        self.commit_calls += 1
        return super().commit(*args, **kwargs)


@pytest.mark.parametrize('entrypoint', ['turn', 'ready', 'bounded'])
@pytest.mark.parametrize('legacy_due_tick', [0, 100])
def test_old_npc_reply_save_rejected_before_any_side_effect(entrypoint, legacy_due_tick):
    package = ScenarioPackage.load(Path(__file__).parents[1] / 'examples/freeform')
    store, model = RecordingStore(), CountingModel()
    package.seed_game(store, 'old-save')
    # A supported reply sorts before the old work. Reject the entire save before
    # draining it, including old work that will become ready in a future turn.
    supported = PendingWork('a-ready', 'single_npc_reply', 0, 20, 'seed',
                            {'actor_id': 'dockhand', 'speech': 'Saved speech.'})
    legacy = PendingWork('z-legacy', 'npc_reply', legacy_due_tick, 10, 'seed',
                         {'actor_id': 'dockhand', 'player_message': 'Old input'})
    event = WorldEvent('seed', 'test_seed', None, None, 0, ())
    store.commit('old-save', 0, event, (), (supported, legacy))
    before = deepcopy(store._games['old-save'])
    store.commit_calls = 0
    engine = TurnEngine(store, package, model)
    progress = []

    async def observe(event):
        progress.append(event)

    async def execute():
        if entrypoint == 'turn':
            return await engine.run_turn(
                TurnInput('old-save', 'Hello', 'new-turn', package.version), progress=observe)
        if entrypoint == 'ready':
            return await engine.run_ready_work('old-save', progress=observe)
        return await engine.session.run_turn_bounded(
            'old-save', 'Hello', 'new-turn', max_tick=0, progress=observe)

    with pytest.raises(ValueError, match=r'Unsupported legacy save.*npc_reply.*new game'):
        asyncio.run(execute())
    assert model.calls == 0
    assert store.commit_calls == 0
    assert store._games['old-save'] == before
    assert progress == []


def test_saved_single_npc_reply_delivers_once_without_generation_and_keeps_context():
    package = ScenarioPackage.load(Path(__file__).parents[1] / 'examples/freeform')
    store, model = InMemoryGameStore(), CountingModel()
    package.seed_game(store, 'current-save')
    event = WorldEvent('input', 'player_input', 'player', None, 0, (), {'text': 'Hello'})
    work = PendingWork('saved-reply', 'single_npc_reply', 0, 10, 'input',
                       {'actor_id': 'dockhand', 'speech': 'Saved speech.', 'player_message': 'Hello'})
    store.commit('current-save', 0, event, (), (work,))
    engine = TurnEngine(store, package, model)

    result = asyncio.run(engine.run_ready_work('current-save'))
    assert result.processed_work_ids == ('saved-reply',)
    assert store.dialogue_history_for_actor('current-save', 'dockhand') == [('Hello', 'Saved speech.')]
    assert any('Saved speech.' in entry.content
               for entry in store.agent_context_entries('current-save', 'dockhand'))
    assert any('Saved speech.' in observation.content
               for observation in store.observations_for('current-save', 'player'))
    before = deepcopy(store._games['current-save'])
    replay = asyncio.run(engine.run_ready_work('current-save'))
    assert replay.processed_work_ids == ()
    assert replay.snapshot == result.snapshot
    assert store._games['current-save'] == before
    assert model.calls == 0
