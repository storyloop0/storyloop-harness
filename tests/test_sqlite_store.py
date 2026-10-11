import pytest
from storyloop_harness.core.contracts import Snapshot, WorldEvent, Effect, Observation, PendingWork, AgentContextCheckpoint


def store(path):
    from storyloop_harness.storage import SQLiteGameStore
    return SQLiteGameStore(path)


def event(id='e', **kw):
    return WorldEvent(id, kw.get('kind', 'player_input'), 'player', None, 1,
                      (Effect(('value',), 1),), {'text': 'hello', 'target_ids': ['a']})


def test_reopen_and_detachment(tmp_path):
    path = tmp_path / 'game.db'
    data = {'value': 0, 'nested': {'a': []}}
    with store(path) as s:
        s.create_game(Snapshot('g', 0, 0, data), (PendingWork('w', 'npc', 1, 2, None, {'ids': ['a']}),))
        data['nested']['a'].append('changed')
        s.commit('g', 0, event(), (Observation('o', 'e', 'a', 'speech', 'hello', 1),), ())
        s.save_agent_context_checkpoint('g', 'a', -1, AgentContextCheckpoint(1, 'summary'))
    with store(path) as s:
        assert s.load('g') == Snapshot('g', 1, 1, {'value': 1, 'nested': {'a': []}})
        assert s.event_exists('g', 'e')
        details = s.event_details('g', 'e'); details['target_ids'].append('b')
        assert s.event_details('g', 'e')['target_ids'] == ['a']
        assert s.player_inputs_for('g')[0].target_ids == ('a',)
        assert [o.content for o in s.observations_for('g', 'a')] == ['hello']
        assert s.observations_for('g', 'b') == []
        assert [e.content for e in s.agent_context_entries('g', 'a')] == ['hello']
        assert s.agent_context_checkpoint('g', 'a') == AgentContextCheckpoint(1, 'summary')
        assert s.agent_context_checkpoint('g', 'b') == AgentContextCheckpoint()
        assert [w.work_id for w in s.ready_work('g', 1)] == ['w']


def test_failed_commit_rolls_back_consumption_and_partial_rows(tmp_path):
    with store(tmp_path / 'game.db') as s:
        s.create_game(Snapshot('g', 0, 0, {'value': 0}), (PendingWork('w', 'npc', 1, 0, None, {}),))
        with pytest.raises(ValueError):
            s.commit('g', 0, event(), (Observation('o', 'e', 'a', 'speech', 'ok', 1), Observation('bad', 'missing', 'a', 'speech', 'bad', 1)), (), 'w')
        assert s.load('g').version == 0
        assert not s.event_exists('g', 'e')
        assert s.observations_for('g', 'a') == []
        assert len(s.pending_work('g')) == 1
        s.commit('g', 0, event(), (), (), 'w')
        with pytest.raises(ValueError):
            s.commit('g', 1, None, (), (), 'w')
        with pytest.raises(ValueError):
            s.commit('g', 1, None, (), (PendingWork('w', 'npc', 2, 0, None, {}),))


def test_independent_connections_cas_and_checkpoint(tmp_path):
    path = tmp_path / 'game.db'
    with store(path) as first, store(path) as second:
        first.create_game(Snapshot('g', 0, 0, {'value': 0}))
        second.commit('g', 0, event(), (), ())
        with pytest.raises(ValueError, match='version changed'):
            first.commit('g', 0, event('stale'), (), ())
        assert not first.event_exists('g', 'stale')
        first.save_agent_context_checkpoint('g', 'a', -1, AgentContextCheckpoint(1, 'first'))
        with pytest.raises(ValueError, match='checkpoint changed'):
            second.save_agent_context_checkpoint('g', 'a', -1, AgentContextCheckpoint(2, 'second'))
        assert second.agent_context_checkpoint('g', 'a').summary == 'first'


def test_create_atomic_order_and_limits(tmp_path):
    with store(tmp_path / 'game.db') as s:
        work = PendingWork('w', 'npc', 1, 0, None, {})
        with pytest.raises(ValueError):
            s.create_game(Snapshot('g', 0, 0, {'value': 0}), (work, work))
        with pytest.raises(KeyError):
            s.load('g')
        s.create_game(Snapshot('g', 0, 0, {'value': 0}), (work, PendingWork('a', 'npc', 1, 1, None, {})))
        assert [w.work_id for w in s.pending_work('g')] == ['a', 'w']
        with pytest.raises(ValueError):
            s.observations_for('g', 'a', limit=0)
        with pytest.raises(ValueError):
            s.save_agent_context_checkpoint('g', 'a', -1, AgentContextCheckpoint(0, ' '))


def test_history_owner_order_counts_and_duplicate_rollback(tmp_path):
    with store(tmp_path / 'game.db') as s:
        s.create_game(Snapshot('g', 0, 0, {'value': 0}))
        s.create_game(Snapshot('other', 0, 0, {'value': 0}))
        s.commit('g', 0, event(), (Observation('o1', 'e', 'a', 'speech', 'hello', 1), Observation('o2', 'e', 'a', 'cue', 'second', 1)), ())
        reply = WorldEvent('reply', 'npc_spoke', 'a', 'e', 1, (), {'player_message': 'hello', 'speech': 'reply'})
        s.commit('g', 1, reply, (), ())
        s.commit('g', 2, WorldEvent('done', 'campaign_turn_completed', None, 'e', 1, (), {}), (), ())
        assert s.completed_turn_count('g') == 1
        assert s.completed_turn_count('other') == 0
        assert s.dialogue_history_for_actor('g', 'a') == [('hello', 'reply')]
        assert s.dialogue_history_for_actor('g', 'b') == []
        assert [e.content for e in s.agent_context_entries('g', 'a')] == ['hello', 'second', '你当时回答：reply']
        assert [e.content for e in s.agent_context_entries('g', 'a', 1)] == ['你当时回答：reply']
        assert [e.content for e in s.agent_context_entries('g', 'player')] == ['hello']
        assert s.agent_context_entries('other', 'a') == []
        assert [o.content for o in s.observations_for('g', 'a', limit=1)] == ['second']
        assert len(s.player_inputs_for('g', limit=1)) == 1
        with pytest.raises(ValueError, match='duplicate event'):
            s.commit('g', 3, event(), (), ())
        with pytest.raises(ValueError, match='duplicate observation'):
            s.commit('g', 3, event('next'), (Observation('o1', 'next', 'a', 'speech', 'bad', 1),), ())
        assert not s.event_exists('g', 'next')
        assert s.load('g').version == 3
        with pytest.raises(ValueError, match='unknown effect path'):
            s.commit('g', 3, WorldEvent('bad', 'action', 'player', None, 1, (Effect(('absent',), 1),)), (), ())
        assert not s.event_exists('g', 'bad')


def test_concurrent_connections_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path = tmp_path / 'game.db'
    with store(path) as s:
        s.create_game(Snapshot('g', 0, 0, {'value': 0}))
    barrier = Barrier(2)
    def write(id):
        with store(path) as s:
            barrier.wait(timeout=10)
            try:
                s.commit('g', 0, event(id), (), ())
                return 'committed'
            except ValueError as exc:
                return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, id) for id in ('one', 'two')]
        assert sorted(f.result(timeout=15) for f in futures) == ['committed', 'game state version changed']
    with store(path) as s:
        assert s.load('g').version == 1
        assert sum(s.event_exists('g', id) for id in ('one', 'two')) == 1


def test_json_failure_rolls_back_and_closed_store_is_unusable(tmp_path):
    import sqlite3
    s = store(':memory:')
    s.create_game(Snapshot('g', 0, 0, {'value': 0}), (PendingWork('w', 'npc', 0, 0, None, {}),))
    with pytest.raises(TypeError):
        s.commit('g', 0, event(), (), (PendingWork('bad', 'npc', 0, 0, None, {'object': object()}),), 'w')
    assert s.load('g').version == 0
    assert [w.work_id for w in s.pending_work('g')] == ['w']
    s.close()
    s.close()
    with pytest.raises(sqlite3.ProgrammingError):
        s.load('g')
