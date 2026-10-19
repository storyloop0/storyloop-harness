"""Standard-library SQLite implementation of the GameStore port."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from storyloop_harness.core.contracts import (
    AgentContextCheckpoint, AgentContextEntry, ContextEvidence, Observation, PendingWork,
    PlayerInput, Snapshot, WorldEvent, Effect,
)
from storyloop_harness.core.state import apply_event
from storyloop_harness.core.checkpoints import validate_checkpoint
from storyloop_harness.core.context_evidence import context_entries
from storyloop_harness.core.generated_scene import (
    GeneratedSceneConflict, GeneratedSceneCorrupt, GeneratedSceneGenerationMarker,
    GeneratedSceneSnapshot, validate_event_snapshot_reference,
)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _event(payload):
    data = json.loads(payload)
    data['effects'] = tuple(Effect(tuple(e['path']), e['value']) for e in data['effects'])
    return WorldEvent(**data)


class SQLiteGameStore:
    """Durable JSON game store. Each write holds an SQLite transaction.

    Independent connections serialize writes and detect stale state versions;
    callers remain responsible for retry policy and task ownership.
    """

    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.execute('PRAGMA foreign_keys = ON')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                tick INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                game_id TEXT NOT NULL REFERENCES games(game_id),
                event_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (game_id, event_id));
            CREATE TABLE IF NOT EXISTS observations (
                game_id TEXT NOT NULL, observation_id TEXT NOT NULL,
                event_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (game_id, observation_id),
                FOREIGN KEY (game_id, event_id) REFERENCES events(game_id, event_id));
            CREATE TABLE IF NOT EXISTS work (
                game_id TEXT NOT NULL REFERENCES games(game_id), work_id TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
                PRIMARY KEY (game_id, work_id));
            CREATE TABLE IF NOT EXISTS checkpoints (
                game_id TEXT NOT NULL REFERENCES games(game_id), actor_id TEXT NOT NULL,
                version INTEGER NOT NULL, summary TEXT NOT NULL,
                scenario_id TEXT NOT NULL, scenario_version TEXT NOT NULL,
                source_ids TEXT NOT NULL,
                PRIMARY KEY (game_id, actor_id));
            CREATE TABLE IF NOT EXISTS generated_scene_snapshots (
                game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                canonical_json TEXT NOT NULL, checksum TEXT NOT NULL,
                PRIMARY KEY (game_id, turn_id));
            CREATE TABLE IF NOT EXISTS generated_scene_generation_markers (
                game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL, marker_id TEXT NOT NULL,
                canonical_json TEXT NOT NULL, checksum TEXT NOT NULL,
                PRIMARY KEY (game_id, turn_id));
        ''')

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _transaction(self):
        self._db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self._db.execute('COMMIT')
        except BaseException:
            self._db.execute('ROLLBACK')
            raise

    def create_game(self, snapshot, initial_work=()):
        with self._transaction():
            if self._db.execute('SELECT 1 FROM games WHERE game_id=?', (snapshot.game_id,)).fetchone():
                raise ValueError('game already exists')
            self._db.execute('INSERT INTO games VALUES (?,?,?,?)',
                             (snapshot.game_id, snapshot.version, snapshot.tick, _encode(snapshot.data)))
            self._add_work(snapshot.game_id, initial_work)

    def generated_scene_generation_marker(self, game_id, turn_id):
        row = self._db.execute(
            'SELECT marker_id,canonical_json,checksum FROM generated_scene_generation_markers '
            'WHERE game_id=? AND turn_id=?', (game_id, turn_id)).fetchone()
        if row is None:
            return None
        marker = GeneratedSceneGenerationMarker.from_storage(row[1], row[2])
        if (marker.marker_id, marker.game_id, marker.turn_id) != (row[0], game_id, turn_id):
            raise GeneratedSceneCorrupt('generated scene generation marker row identity mismatch')
        return marker

    def create_generated_scene_generation_marker(self, marker):
        validated = GeneratedSceneGenerationMarker.from_storage(marker.canonical_json, marker.checksum)
        if validated != marker:
            raise GeneratedSceneCorrupt('generated scene generation marker wrapper mismatch')
        with self._transaction():
            self.load(validated.game_id)
            if self.generated_scene_generation_marker(validated.game_id, validated.turn_id) is not None:
                return False
            self._db.execute('INSERT INTO generated_scene_generation_markers VALUES (?,?,?,?,?)',
                             (validated.game_id, validated.turn_id, validated.marker_id,
                              validated.canonical_json, validated.checksum))
        return True

    def load(self, game_id):
        row = self._db.execute('SELECT version,tick,data FROM games WHERE game_id=?', (game_id,)).fetchone()
        if row is None:
            raise KeyError(game_id)
        return Snapshot(game_id, row[0], row[1], json.loads(row[2]))

    def generated_scene_snapshot(self, game_id, turn_id):
        row = self._db.execute('SELECT snapshot_id,canonical_json,checksum FROM generated_scene_snapshots '
                               'WHERE game_id=? AND turn_id=?', (game_id, turn_id)).fetchone()
        if row is None:
            return None
        record = GeneratedSceneSnapshot.from_storage(row[1], row[2])
        if (record.snapshot_id, record.game_id, record.turn_id) != (row[0], game_id, turn_id):
            from storyloop_harness.core.generated_scene import GeneratedSceneCorrupt
            raise GeneratedSceneCorrupt('generated scene row identity mismatch')
        return record

    def save_generated_scene_snapshot(self, snapshot):
        record = GeneratedSceneSnapshot.from_storage(snapshot.canonical_json, snapshot.checksum)
        if record != snapshot:
            from storyloop_harness.core.generated_scene import GeneratedSceneCorrupt
            raise GeneratedSceneCorrupt('generated scene wrapper mismatch')
        with self._transaction():
            self.load(record.game_id)
            prior = self.generated_scene_snapshot(record.game_id, record.turn_id)
            if prior is not None:
                if prior != record:
                    raise GeneratedSceneConflict('generated scene already exists')
                return prior
            self._db.execute('INSERT INTO generated_scene_snapshots VALUES (?,?,?,?,?)',
                             (record.game_id, record.turn_id, record.snapshot_id,
                              record.canonical_json, record.checksum))
            return self.generated_scene_snapshot(record.game_id, record.turn_id)

    def _events(self, game_id):
        self.load(game_id)
        return [(version, _event(payload)) for version, payload in self._db.execute(
            'SELECT version,payload FROM events WHERE game_id=? ORDER BY version,rowid', (game_id,))]

    def event_exists(self, game_id, event_id):
        self.load(game_id)
        return self._db.execute('SELECT 1 FROM events WHERE game_id=? AND event_id=?', (game_id, event_id)).fetchone() is not None

    def event_details(self, game_id, event_id):
        self.load(game_id)
        row = self._db.execute('SELECT payload FROM events WHERE game_id=? AND event_id=?', (game_id, event_id)).fetchone()
        return _event(row[0]).details if row else None

    def completed_turn_count(self, game_id):
        return sum(e.kind == 'campaign_turn_completed' for _, e in self._events(game_id))

    def _add_work(self, game_id, items):
        for work in items:
            try:
                self._db.execute('INSERT INTO work (game_id,work_id,payload) VALUES (?,?,?)',
                                 (game_id, work.work_id, _encode(asdict(work))))
            except sqlite3.IntegrityError as exc:
                raise ValueError('duplicate work') from exc

    def commit(self, game_id, expected_version, event, observations, new_work, consumed_work_id=None):
        with self._transaction():
            if event is not None and any(k in event.details for k in (
                    'generated_scene_snapshot_id', 'generated_scene_snapshot_checksum')):
                validate_event_snapshot_reference(event, self.generated_scene_snapshot(
                    game_id, event.event_id.rsplit(':', 1)[0]))
            before = self.load(game_id)
            if before.version != expected_version:
                raise ValueError('game state version changed')
            if consumed_work_id is not None:
                changed = self._db.execute('UPDATE work SET consumed=1 WHERE game_id=? AND work_id=? AND consumed=0',
                                          (game_id, consumed_work_id)).rowcount
                if not changed:
                    raise ValueError('pending work was already consumed or does not exist')
            after = apply_event(before, event) if event else before
            if event:
                try:
                    self._db.execute('INSERT INTO events VALUES (?,?,?,?)',
                                     (game_id, event.event_id, after.version, _encode(asdict(event))))
                except sqlite3.IntegrityError as exc:
                    raise ValueError('duplicate event') from exc
            for observation in observations:
                if not self.event_exists(game_id, observation.event_id):
                    raise ValueError('observation refers to an unknown event')
                source_version = self._db.execute(
                    'SELECT version FROM events WHERE game_id=? AND event_id=?',
                    (game_id, observation.event_id)).fetchone()[0]
                cursor = self._db.execute(
                    'SELECT version FROM checkpoints WHERE game_id=? AND actor_id=?',
                    (game_id, observation.recipient_id)).fetchone()
                if cursor is not None and source_version <= cursor[0]:
                    raise ValueError('observation refers to a checkpointed event')
                try:
                    self._db.execute('INSERT INTO observations VALUES (?,?,?,?)',
                                     (game_id, observation.observation_id, observation.event_id, _encode(asdict(observation))))
                except sqlite3.IntegrityError as exc:
                    raise ValueError('duplicate observation') from exc
            self._add_work(game_id, new_work)
            self._db.execute('UPDATE games SET version=?,tick=?,data=? WHERE game_id=?',
                             (after.version, after.tick, _encode(after.data), game_id))
            return self.load(game_id)

    @staticmethod
    def _limit(items, limit):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError('history limit must be a positive integer')
        return items[-limit:] if limit is not None else items

    def observations_for(self, game_id, recipient_id, *, limit=None):
        self.load(game_id)
        rows = self._db.execute('''SELECT o.payload FROM observations o JOIN events e
            ON o.game_id=e.game_id AND o.event_id=e.event_id
            WHERE o.game_id=? ORDER BY e.version,o.rowid''', (game_id,))
        items = [Observation(**json.loads(row[0])) for row in rows]
        return self._limit([o for o in items if o.recipient_id == recipient_id], limit)

    def dialogue_history_for_actor(self, game_id, actor_id):
        return [(e.details['player_message'], e.details['speech']) for _, e in self._events(game_id)
                if e.kind == 'npc_spoke' and e.actor_id == actor_id]

    def player_inputs_for(self, game_id, *, limit=None):
        channels = {'player_input': 'speech', 'player_query': 'query', 'player_action': 'action', 'action_rejected': 'action'}
        return self._limit([PlayerInput(e.event_id, e.tick, e.details['text'],
                                       e.details.get('channel', channels[e.kind]), tuple(e.details.get('target_ids', [])))
                            for _, e in self._events(game_id) if e.actor_id == 'player' and e.kind in channels], limit)

    def agent_context_entries(self, game_id, actor_id, after_version=-1):
        # The event/observation merge must use one consistent database view.
        self._db.execute('BEGIN')
        try:
            return self._context_entries(game_id, actor_id, after_version)
        finally:
            self._db.execute('ROLLBACK')

    def _context_entries(self, game_id, actor_id, after_version):
        return context_entries(self._events(game_id), self.observations_for(game_id, actor_id),
                               actor_id, after_version)

    def agent_context_checkpoint(self, game_id, actor_id):
        self.load(game_id)
        row = self._db.execute('SELECT version,summary,scenario_id,scenario_version,source_ids FROM checkpoints WHERE game_id=? AND actor_id=?', (game_id, actor_id)).fetchone()
        if not row:
            return AgentContextCheckpoint()
        source_ids = tuple(json.loads(row[4]))
        return AgentContextCheckpoint(*row[:4], source_ids,
            ContextEvidence('unverified_summary', source_ids, actor_id))

    def save_agent_context_checkpoint(self, game_id, actor_id, expected_version, checkpoint):
        with self._transaction():
            if self.agent_context_checkpoint(game_id, actor_id).through_version != expected_version:
                raise ValueError('agent context checkpoint changed')
            validate_checkpoint(self.load(game_id), self._context_entries(game_id, actor_id, expected_version),
                                expected_version, checkpoint)
            self._db.execute('''INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(game_id,actor_id) DO UPDATE SET version=excluded.version,
                summary=excluded.summary,scenario_id=excluded.scenario_id,
                scenario_version=excluded.scenario_version,source_ids=excluded.source_ids''',
                (game_id, actor_id, checkpoint.through_version, checkpoint.summary,
                 checkpoint.scenario_id, checkpoint.scenario_version, _encode(checkpoint.source_ids)))

    def pending_work(self, game_id):
        self.load(game_id)
        items = [PendingWork(**json.loads(row[0])) for row in self._db.execute(
            'SELECT payload FROM work WHERE game_id=? AND consumed=0', (game_id,))]
        return sorted(items, key=lambda w: (w.due_tick, -w.mandatory, -w.priority, w.work_id))

    def ready_work(self, game_id, tick):
        return [w for w in self.pending_work(game_id) if w.due_tick <= tick]
