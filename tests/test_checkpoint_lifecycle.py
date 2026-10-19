"""Long-game checkpoint lifecycle against the durable store."""
import asyncio

import pytest

from scene_fixtures import source_package
from storyloop_harness import TurnEngine, TurnInput
from storyloop_harness.context.checkpoints import CheckpointLifecycle
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.core.contracts import AgentContextCheckpoint, Effect, Observation, WorldEvent
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import OfflineModel
from storyloop_harness.generation import ExecutionRequestLimitExceeded, ExecutionTimeout


def test_checkpoint_lifecycle_has_public_integration_imports():
    from storyloop_harness.context import CheckpointLifecycle as PublicLifecycle
    from storyloop_harness.context import CheckpointRequest
    from storyloop_harness.advanced import validate_checkpoint

    assert PublicLifecycle is CheckpointLifecycle
    assert CheckpointRequest.__name__ == 'CheckpointRequest'
    assert callable(validate_checkpoint)


def seed(store, package, game_id, count, *, start=0):
    if start == 0:
        package.seed_game(store, game_id)
    for index in range(start, start + count):
        event = WorldEvent(f'{game_id}-event-{index}', 'synthetic', None, None, index, ())
        clue = 'MIDDLE_CLUE' if index == 3 else f'ordinary {index}'
        store.commit(game_id, store.load(game_id).version, event, (
            Observation(f'{game_id}-player-{index}', event.event_id, 'player', 'scene', clue, index),
            Observation(f'{game_id}-guide-{index}', event.event_id, 'guide', 'private_message', f'GUIDE_SECRET {index}', index),
            Observation(f'{game_id}-guard-{index}', event.event_id, 'guard', 'private_message', f'GUARD_SECRET {index}', index),
        ), ())


def test_checkpoint_compresses_complete_versions_and_keeps_scoped_sources(tmp_path):
    async def run():
        package = source_package()
        path = tmp_path / 'long.db'
        with SQLiteGameStore(path) as store:
            seed(store, package, 'game', 24)
            before = store.load('game')
            requests = []

            async def summarize(request):
                requests.append(request)
                return ' | '.join((request.previous_summary, *(item.content for item in request.entries))).strip(' |')

            lifecycle = CheckpointLifecycle(store, package, summarize)
            assert await lifecycle.refresh('game', 'guide') is True
            checkpoint = store.agent_context_checkpoint('game', 'guide')
            assert checkpoint.through_version == 16
            assert checkpoint.scenario_id == package.package_id
            assert checkpoint.scenario_version == package.version
            assert checkpoint.source_ids == tuple(f'game-guide-{index}' for index in range(16))
            assert 'GUARD_SECRET' not in checkpoint.summary
            assert 'MIDDLE_CLUE' not in checkpoint.summary
            assert [item.state_version for item in store.agent_context_entries('game', 'guide', 16)] == list(range(17, 25))
            assert len(store.agent_context_entries('game', 'guide')) == 24
            assert store.load('game') == before
            assert await lifecycle.refresh('game', 'guide') is False
            assert await lifecycle.refresh('game', 'player') is True
            player = store.agent_context_checkpoint('game', 'player')
            assert 'MIDDLE_CLUE' in player.summary
            assert 'GUIDE_SECRET' not in player.summary
            change = WorldEvent('world-change', 'synthetic', None, None, 24,
                                (Effect(('actors', 'guard', 'location'), 'elsewhere'),))
            store.commit('game', store.load('game').version, change, (), ())
            projected = SceneContextProjector(store, package).project(store.load('game'), 'Guide')
            assert 'GUIDE_SECRET' in projected.request['npc_contexts'][0]['own_summary']['text']
            assert projected.request['npc_contexts'][0]['own_summary']['evidence']['kind'] == 'unverified_summary'
            assert 'GUARD_SECRET' not in str(projected.request)
            assert store.load('game').data['actors']['guard']['location'] == 'elsewhere'
            assert 'guard' not in projected.request['nearby_people']
            assert requests[0].actor_id == 'guide'
            assert requests[0].previous_summary == ''
        with SQLiteGameStore(path) as reopened:
            assert reopened.agent_context_checkpoint('game', 'guide') == checkpoint
            assert [item.entry_id for item in reopened.agent_context_entries('game', 'guide')] == [
                f'game-guide-{index}' for index in range(24)]

    asyncio.run(run())


def test_checkpoint_retains_whole_version_group_and_rejects_oversized_summary(tmp_path):
    async def run():
        package = source_package()
        with SQLiteGameStore(tmp_path / 'groups.db') as store:
            package.seed_game(store, 'game')
            for index in range(16):
                event = WorldEvent(f'e-{index}', 'synthetic', None, None, index, ())
                observations = [Observation(f'o-{index}', event.event_id, 'guide', 'scene', str(index), index)]
                if index == 8:
                    observations.extend(Observation(f'extra-{part}', event.event_id, 'guide', 'scene',
                                                    f'group {part}', index) for part in range(3))
                store.commit('game', store.load('game').version, event, tuple(observations), ())

            async def oversized(_request):
                return 'x' * 1201

            with pytest.raises(ValueError, match='invalid summary'):
                await CheckpointLifecycle(store, package, oversized).refresh('game', 'guide')
            assert store.agent_context_checkpoint('game', 'guide') == AgentContextCheckpoint()
            captured = []

            async def summarize(request):
                captured.extend(request.entries)
                return 'group summary'

            assert await CheckpointLifecycle(store, package, summarize).refresh('game', 'guide')
            checkpoint = store.agent_context_checkpoint('game', 'guide')
            assert checkpoint.through_version == 8
            assert {entry.state_version for entry in captured} == set(range(1, 9))
            assert [entry.entry_id for entry in store.agent_context_entries('game', 'guide', 8)][:4] == [
                'o-8', 'extra-0', 'extra-1', 'extra-2']

    asyncio.run(run())


def test_failure_retry_and_incremental_summary_do_not_skip_events(tmp_path):
    async def run():
        package = source_package()
        with SQLiteGameStore(tmp_path / 'retry.db') as store:
            seed(store, package, 'game', 24)
            attempts = []

            async def summarize(request):
                attempts.append(request)
                if len(attempts) == 1:
                    raise RuntimeError('synthetic summary failure')
                return f'{request.previous_summary} ' + ' '.join(item.content for item in request.entries)

            lifecycle = CheckpointLifecycle(store, package, summarize)
            with pytest.raises(RuntimeError, match='synthetic summary failure'):
                await lifecycle.refresh('game', 'player')
            assert store.agent_context_checkpoint('game', 'player') == AgentContextCheckpoint()
            assert 'MIDDLE_CLUE' in [row.content for row in store.agent_context_entries('game', 'player')]
            assert await lifecycle.refresh('game', 'player') is True
            assert attempts[0].entries == attempts[1].entries
            first = store.agent_context_checkpoint('game', 'player')
            seed(store, package, 'game', 16, start=24)
            assert await lifecycle.refresh('game', 'player') is True
            second = store.agent_context_checkpoint('game', 'player')
            assert attempts[-1].previous_summary == first.summary
            assert second.through_version == 32
            assert second.source_ids == tuple(f'game-player-{index}' for index in range(16, 32))
            assert 'MIDDLE_CLUE' in second.summary
            assert await lifecycle.refresh('game', 'player') is False

    asyncio.run(run())


def test_checkpoint_rejects_future_cursor_wrong_scenario_and_cross_game(tmp_path):
    package = source_package()
    with SQLiteGameStore(tmp_path / 'scope.db') as store:
        seed(store, package, 'one', 1)
        seed(store, package, 'two', 1)
        valid = AgentContextCheckpoint(1, 'summary', package.package_id, package.version, ('one-guide-0',))
        with pytest.raises(ValueError, match='source'):
            store.save_agent_context_checkpoint('one', 'guide', -1,
                AgentContextCheckpoint(1, 'summary', package.package_id, package.version, ('two-guide-0',)))
        with pytest.raises(ValueError, match='cursor'):
            store.save_agent_context_checkpoint('one', 'guide', -1,
                AgentContextCheckpoint(2, 'summary', package.package_id, package.version, ('one-guide-0',)))
        with pytest.raises(ValueError, match='scenario'):
            store.save_agent_context_checkpoint('one', 'guide', -1,
                AgentContextCheckpoint(1, 'summary', 'wrong', package.version, ('one-guide-0',)))
        store.save_agent_context_checkpoint('one', 'guide', -1, valid)
        assert store.agent_context_checkpoint('two', 'guide') == AgentContextCheckpoint()
        with pytest.raises(ValueError, match='checkpointed event'):
            store.commit('one', store.load('one').version, None,
                (Observation('late', 'one-event-0', 'guide', 'scene', 'late clue', 1),), ())
        assert all(item.entry_id != 'late' for item in store.agent_context_entries('one', 'guide'))


def test_engine_runs_optional_maintenance_after_turn_and_keeps_turn_on_failure(tmp_path):
    async def run():
        package = source_package()
        with SQLiteGameStore(tmp_path / 'engine.db') as store:
            seed(store, package, 'game', 24)
            calls = []

            async def summarize(request):
                calls.append(request.actor_id)
                if request.actor_id == 'guide':
                    raise RuntimeError('checkpoint failure')
                return 'memory of ' + request.entries[0].content

            engine = TurnEngine(store, package, OfflineModel(), checkpoint_summarizer=summarize)
            outcome = await engine.run_turn(TurnInput('game', 'Guide hello', 'turn-1', package.version))
            assert outcome.snapshot.version > 24
            assert store.agent_context_checkpoint('game', 'player').through_version >= 16
            assert store.agent_context_checkpoint('game', 'guide') == AgentContextCheckpoint()
            assert 'guide' in calls
            before_replay = tuple(calls)
            replay = await engine.run_turn(TurnInput('game', 'Guide hello', 'turn-1', package.version))
            assert replay.model_usage == ()
            assert tuple(calls) == before_replay

    asyncio.run(run())


@pytest.mark.parametrize('error', [ExecutionRequestLimitExceeded, ExecutionTimeout])
def test_checkpoint_maintenance_rethrows_execution_error(tmp_path, error):
    async def run():
        package = source_package()
        with SQLiteGameStore(tmp_path / 'execution.db') as store:
            seed(store, package, 'game', 24)
            async def summarize(_request):
                raise error()
            engine = TurnEngine(store, package, OfflineModel(), checkpoint_summarizer=summarize)
            with pytest.raises(error):
                await engine.run_turn(TurnInput('game', 'Guide hello', 'turn-1', package.version))
    asyncio.run(run())
