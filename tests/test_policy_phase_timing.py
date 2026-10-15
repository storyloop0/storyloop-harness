"""Phase boundaries and failure evidence for the policy comparison example."""
import asyncio
from dataclasses import replace
import importlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from examples.evaluation.task import Fixture
from examples.policy_comparison.cases import CASES
from storyloop_harness.context import ContextPolicy


subject = importlib.import_module('examples.policy_comparison.task')
PHASES = ('preparation', 'projection', 'generation', 'turn', 'replay')


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, value):
        self.value += value


def _run(**kwargs):
    return asyncio.run(subject.task(case=CASES[0], policy=ContextPolicy.DEFAULT, **kwargs))


def test_phase_boundaries_exclude_later_evidence_work(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(subject, 'perf_counter', clock)
    monkeypatch.setattr(subject, '_revision', lambda: None)
    real_package = subject._package
    real_project = subject.TurnEngine.project_context
    real_run = subject.TurnEngine.run_turn
    real_summary = subject.summarize_attempt_usage
    real_deepcopy = subject.deepcopy
    calls = {'projection': 0, 'preflight': 0, 'turn': 0, 'summary': 0,
             'inside_turn': False}

    def package(*args):
        result = real_package(*args)
        clock.advance(2)
        return result

    def project(self, *args):
        result = real_project(self, *args)
        calls['projection'] += 1
        if not calls['inside_turn']:
            calls['preflight'] += 1
            clock.advance(3)
        return result

    async def run_turn(self, *args):
        calls['turn'] += 1
        if calls['turn'] == 1:
            clock.advance(5)
        calls['inside_turn'] = True
        try:
            result = await real_run(self, *args)
        finally:
            calls['inside_turn'] = False
        if calls['turn'] == 1:
            clock.advance(7)
        return result

    def summary(*args):
        calls['summary'] += 1
        if calls['summary'] > 1:
            clock.advance(101)
        return real_summary(*args)

    def deepcopy(*args):
        clock.advance(103)
        return real_deepcopy(*args)

    class Model(Fixture):
        async def __call__(self, messages, **kwargs):
            clock.advance(11)
            return await super().__call__(messages, **kwargs)

    monkeypatch.setattr(subject, '_package', package)
    monkeypatch.setattr(subject.TurnEngine, 'project_context', project)
    monkeypatch.setattr(subject.TurnEngine, 'run_turn', run_turn)
    monkeypatch.setattr(subject, 'summarize_attempt_usage', summary)
    monkeypatch.setattr(subject, 'deepcopy', deepcopy)
    row = _run(model_factory=Model)
    assert row['passed'] is True
    assert {phase: row[phase + '_seconds'] for phase in PHASES[:4]} == {
        'preparation': 5, 'projection': 3, 'generation': 11, 'turn': 23}
    assert tuple(row['timing_status'][p] for p in PHASES) == (
        'completed', 'completed', 'completed', 'completed', 'completed')
    assert row['replay_seconds'] >= 0
    assert row['metadata']['timing_version'] == 'policy-phases-v1'
    assert calls['preflight'] == 1
    assert calls['projection'] >= calls['preflight']


@pytest.mark.parametrize('where', ['preparation', 'projection', 'generation'])
@pytest.mark.parametrize('kind', [ValueError, asyncio.CancelledError, KeyboardInterrupt])
def test_phase_failure_identity(monkeypatch, where, kind):
    clock, captured = Clock(), []
    original = kind('PRIVATE_SENTINEL')
    monkeypatch.setattr(subject, 'perf_counter', clock)
    monkeypatch.setattr(subject, '_revision', lambda: None)

    def fail():
        clock.advance(13)
        raise original

    class Model(Fixture):
        async def __call__(self, messages, **kwargs):
            fail()

    def factory():
        if where == 'preparation':
            fail()
        return Model()

    if where == 'projection':
        monkeypatch.setattr(subject.TurnEngine, 'project_context', lambda *args: fail())
    with pytest.raises(kind) as caught:
        _run(model_factory=factory, evidence_sink=captured.append)
    assert caught.value is original
    assert len(captured) == 1
    row = captured[0]
    bad = 'failed' if kind is ValueError else 'cancelled'
    expected = {
        'preparation': (bad, 'not_started', 'not_started', 'not_started', 'not_started'),
        'projection': (bad, bad, 'not_started', 'not_started', 'not_started'),
        'generation': ('completed', 'completed', bad, bad, 'not_started'),
    }[where]
    assert tuple(row['timing_status'][p] for p in PHASES) == expected
    for phase, status in zip(PHASES, expected):
        assert (row[phase + '_seconds'] is None) is (status == 'not_started')
        if status == bad:
            assert row[phase + '_seconds'] == 13
    assert row['error'] == {'type': kind.__name__}
    assert row['passed'] is False
    assert 'PRIVATE_SENTINEL' not in json.dumps(row)


@pytest.mark.parametrize('where', ['TemporaryDirectory', '_seed', 'version'])
def test_preparation_includes_all_setup_sites(monkeypatch, where):
    clock, captured = Clock(), []
    monkeypatch.setattr(subject, 'perf_counter', clock)

    def fail(*args, **kwargs):
        clock.advance(13)
        raise ValueError('PRIVATE_SENTINEL')

    monkeypatch.setattr(subject, where, fail)
    with pytest.raises(ValueError):
        _run(evidence_sink=captured.append)
    assert len(captured) == 1
    row = captured[0]
    assert row['preparation_seconds'] == 13
    assert tuple(row['timing_status'][p] for p in PHASES) == (
        'failed', 'not_started', 'not_started', 'not_started', 'not_started')
    assert all(row[p + '_seconds'] is None for p in PHASES[1:])
    assert row['metadata']['timing_version'] == 'policy-phases-v1'
    assert 'PRIVATE_SENTINEL' not in json.dumps(row)


@pytest.mark.parametrize('kind', [ValueError, asyncio.CancelledError, KeyboardInterrupt])
def test_original_model_error_survives_all_cleanup_errors(monkeypatch, kind):
    original = kind('MODEL_SENTINEL')
    captured, closed = [], []
    real_close = subject.SQLiteGameStore.close
    real_temporary = subject.TemporaryDirectory

    def close(store):
        real_close(store)
        closed.append('sqlite')
        raise RuntimeError('SQLITE_CLOSE_SENTINEL')

    class Temporary(real_temporary):
        def cleanup(self):
            super().cleanup()
            closed.append('temporary')
            raise RuntimeError('TEMP_CLOSE_SENTINEL')

    class Client:
        async def close(self):
            closed.append('client')
            raise RuntimeError('CLIENT_CLOSE_SENTINEL')

    class Model(Fixture):
        def __init__(self):
            self.client = Client()

        async def __call__(self, messages, **kwargs):
            raise original

    monkeypatch.setattr(subject.SQLiteGameStore, 'close', close)
    monkeypatch.setattr(subject, 'TemporaryDirectory', Temporary)
    with pytest.raises(kind) as caught:
        _run(model_mode='sdk_mock', model_factory=Model, evidence_sink=captured.append)
    assert caught.value is original
    assert closed == ['sqlite', 'temporary', 'client']
    assert len(captured) == 1
    row = captured[0]
    bad = 'failed' if kind is ValueError else 'cancelled'
    assert row['timing_status']['generation'] == bad
    assert row['timing_status']['turn'] == bad
    assert row['error'] == {'type': kind.__name__}
    assert 'SENTINEL' not in json.dumps(row)


@pytest.mark.parametrize('kind', [ValueError, asyncio.CancelledError])
def test_original_model_error_survives_client_lookup_error(kind):
    original = kind('MODEL_SENTINEL')
    lookup_error = RuntimeError('CLIENT_LOOKUP_SENTINEL')
    captured = []
    lookups = []

    class Model(Fixture):
        @property
        def client(self):
            lookups.append('client')
            raise lookup_error

        async def __call__(self, messages, **kwargs):
            raise original

    with pytest.raises(kind) as caught:
        _run(model_mode='sdk_mock', model_factory=Model, evidence_sink=captured.append)
    assert caught.value is original
    assert lookups == ['client']
    assert len(captured) == 1
    assert captured[0]['error'] == {'type': kind.__name__}
    assert 'SENTINEL' not in json.dumps(captured[0])


def test_client_lookup_error_after_success_emits_failed_row():
    lookup_error = RuntimeError('CLIENT_LOOKUP_SENTINEL')
    captured = []
    lookups = []

    class Model(Fixture):
        @property
        def client(self):
            lookups.append('client')
            raise lookup_error

    with pytest.raises(RuntimeError) as caught:
        _run(model_mode='sdk_mock', model_factory=Model, evidence_sink=captured.append)
    assert caught.value is lookup_error
    assert lookups == ['client']
    assert len(captured) == 1
    row = captured[0]
    assert row['passed'] is False
    assert row['error'] == {'type': 'RuntimeError'}
    assert tuple(row['timing_status'][p] for p in PHASES) == (
        'completed', 'completed', 'completed', 'completed', 'completed')
    assert 'CLIENT_LOOKUP_SENTINEL' not in json.dumps(row)


@pytest.mark.parametrize('where', ['summary', 'deepcopy', 'sqlite', 'temporary', 'client'])
def test_late_failure_keeps_completed_phase_evidence(monkeypatch, where):
    captured = []
    error = RuntimeError('LATE_SENTINEL')
    real_summary = subject.summarize_attempt_usage
    real_deepcopy = subject.deepcopy
    real_close = subject.SQLiteGameStore.close
    real_temporary = subject.TemporaryDirectory
    summary_calls = 0

    def summary(*args):
        nonlocal summary_calls
        summary_calls += 1
        if where == 'summary' and summary_calls == 2:
            raise error
        return real_summary(*args)

    def deepcopy(*args):
        if where == 'deepcopy':
            raise error
        return real_deepcopy(*args)

    def close(store):
        real_close(store)
        if where == 'sqlite':
            raise error

    class Temporary(real_temporary):
        def cleanup(self):
            super().cleanup()
            if where == 'temporary':
                raise error

    class Client:
        async def close(self):
            if where == 'client':
                raise error

    def factory():
        model = Fixture()
        model.client = Client()
        return model

    monkeypatch.setattr(subject, 'summarize_attempt_usage', summary)
    monkeypatch.setattr(subject, 'deepcopy', deepcopy)
    monkeypatch.setattr(subject.SQLiteGameStore, 'close', close)
    monkeypatch.setattr(subject, 'TemporaryDirectory', Temporary)
    with pytest.raises(RuntimeError) as caught:
        _run(model_mode='sdk_mock', model_factory=factory, evidence_sink=captured.append)
    assert caught.value is error
    assert len(captured) == 1
    row = captured[0]
    replay_status = ('completed' if where in ('temporary', 'client') else 'not_started')
    assert tuple(row['timing_status'][p] for p in PHASES) == (
        'completed', 'completed', 'completed', 'completed', replay_status)
    assert all(row[p + '_seconds'] is not None for p in PHASES[:4])
    assert (row['replay_seconds'] is None) is (replay_status == 'not_started')
    assert row['passed'] is False
    assert row['error'] == {'type': 'RuntimeError'}
    assert 'LATE_SENTINEL' not in json.dumps(row)


def test_sink_failure_cannot_replace_original_error():
    original = ValueError('MODEL_SENTINEL')
    captured = []

    class Model(Fixture):
        async def __call__(self, messages, **kwargs):
            raise original

    def sink(row):
        captured.append(row)
        raise RuntimeError('SINK_SENTINEL')

    with pytest.raises(ValueError) as caught:
        _run(model_factory=Model, evidence_sink=sink)
    assert caught.value is original
    assert len(captured) == 1
    assert captured[0]['error'] == {'type': 'ValueError'}


def test_success_returns_once_without_sending_to_sink():
    captured = []
    row = _run(evidence_sink=captured.append)
    assert row['passed'] is True
    assert captured == []
    assert row['timing_status']['replay'] == 'completed'
    assert row['replay_seconds'] >= 0


def test_replay_reopens_real_sqlite_after_close_and_times_only_replay(monkeypatch):
    clock = Clock()
    stores, paths, engines, inputs, closed = [], [], [], [], []
    real_store, real_engine = subject.SQLiteGameStore, subject.TurnEngine
    monkeypatch.setattr(subject, 'perf_counter', clock)
    monkeypatch.setattr(subject, '_revision', lambda: None)

    class Store(real_store):
        def __init__(self, path):
            if stores:
                with pytest.raises(sqlite3.ProgrammingError):
                    stores[0]._db.execute('SELECT 1')
                clock.advance(2)
            super().__init__(path)
            stores.append(self)
            paths.append(path)

        def close(self):
            super().close()
            closed.append(self)
            clock.advance(101 if self is stores[0] else 7)

    class Engine(real_engine):
        def __init__(self, store, package, model, **kwargs):
            engines.append((store, package, model, kwargs))
            if len(engines) == 2:
                clock.advance(3)
            super().__init__(store, package, model, **kwargs)

        async def run_turn(self, turn):
            inputs.append(turn)
            if len(inputs) == 2:
                clock.advance(5)
            return await super().run_turn(turn)

    monkeypatch.setattr(subject, 'SQLiteGameStore', Store)
    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    row = _run()
    assert row['passed'] is True
    assert len(stores) == len(engines) == len(inputs) == 2
    assert stores[0] is not stores[1] and paths[0] == paths[1]
    assert [item[0] for item in engines] == stores
    assert engines[0][1] is engines[1][1]
    assert engines[0][3] == engines[1][3]
    assert inputs[0] == inputs[1]
    assert closed == stores
    assert row['replay_seconds'] == 17


def test_replay_forbidden_model_cannot_reach_first_delegate(monkeypatch):
    captured, models, engines = [], [], []
    real_engine = subject.TurnEngine
    delegate_calls = 0

    class Delegate(Fixture):
        async def __call__(self, messages, **kwargs):
            nonlocal delegate_calls
            delegate_calls += 1
            return await super().__call__(messages, **kwargs)

    class Engine(real_engine):
        def __init__(self, store, package, model, **kwargs):
            models.append(model)
            engines.append(self)
            super().__init__(store, package, model, **kwargs)

        async def run_turn(self, turn):
            if len(engines) == 2 and self is engines[1]:
                await models[1]([], injected=True)
            return await super().run_turn(turn)

    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    with pytest.raises(RuntimeError) as caught:
        _run(model_factory=Delegate, evidence_sink=captured.append)
    assert str(caught.value) == 'policy replay attempted generation'
    assert delegate_calls == 1
    assert len(models) == 2 and models[0] is not models[1]
    assert len(captured) == 1
    row = captured[0]
    assert row['result']['window_broken'] is True
    assert row['timing_status']['generation'] == row['timing_status']['turn'] == 'completed'
    assert row['timing_status']['replay'] == 'failed'
    assert row['replay']['new_calls'] == 1
    assert row['replay_attempt_count'] == 0
    assert row['checks']['replay'] is False and row['passed'] is False
    assert row['error'] == {'type': 'RuntimeError'}


@pytest.mark.parametrize('site,kind,elapsed,replay_status,closed_count', [
    ('first_close', ValueError, None, 'not_started', 1),
    ('reopen', ValueError, 13, 'failed', 1),
    ('run', ValueError, 13, 'failed', 2),
    ('run', asyncio.CancelledError, 13, 'cancelled', 2),
    ('run', KeyboardInterrupt, 13, 'cancelled', 2),
    ('close', ValueError, 13, 'failed', 2),
    ('run_and_close', ValueError, 20, 'failed', 2),
    ('comparison', ValueError, 13, 'completed', 2),
])
def test_replay_failure_preserves_first_result_and_primary_error(
        monkeypatch, site, kind, elapsed, replay_status, closed_count):
    clock, captured, stores, engines, closed = Clock(), [], [], [], []
    original = kind('PRIVATE_SENTINEL')
    real_store, real_engine = subject.SQLiteGameStore, subject.TurnEngine
    monkeypatch.setattr(subject, 'perf_counter', clock)
    monkeypatch.setattr(subject, '_revision', lambda: None)

    class Store(real_store):
        def __init__(self, path):
            if site == 'reopen' and stores:
                clock.advance(13)
                raise original
            super().__init__(path)
            stores.append(self)

        def close(self):
            super().close()
            closed.append(self)
            if ((site == 'first_close' and self is stores[0])
                    or (site in ('close', 'run_and_close') and self is stores[-1] and len(stores) == 2)):
                clock.advance(7 if site == 'run_and_close' else 13)
                raise (RuntimeError('CLOSE_SENTINEL') if site == 'run_and_close' else original)

    class Engine(real_engine):
        def __init__(self, *args, **kwargs):
            engines.append(self)
            super().__init__(*args, **kwargs)

        async def run_turn(self, turn):
            if self is engines[-1] and len(engines) == 2:
                if site in ('run', 'run_and_close'):
                    clock.advance(13)
                    raise original
                if site == 'comparison':
                    actual = await super().run_turn(turn)
                    clock.advance(13)

                    class FailingEvents:
                        def __len__(self):
                            raise original

                    return SimpleNamespace(events=FailingEvents(), snapshot=actual.snapshot,
                                           narration=actual.narration, model_usage=actual.model_usage)
            return await super().run_turn(turn)

    monkeypatch.setattr(subject, 'SQLiteGameStore', Store)
    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    with pytest.raises(kind) as caught:
        _run(evidence_sink=captured.append)
    assert caught.value is original
    assert len(captured) == 1
    row = captured[0]
    assert row['result']['window_broken'] is True
    assert row['timing_status']['preparation'] == 'completed'
    assert row['timing_status']['projection'] == 'completed'
    assert row['timing_status']['generation'] == 'completed'
    assert row['timing_status']['turn'] == 'completed'
    assert row['timing_status']['replay'] == replay_status
    assert row['replay_seconds'] == elapsed
    assert row['attempt_summary']['attempts'] == []
    assert row.get('replay_attempt_count') == (None if site == 'first_close' else 0)
    assert row['passed'] is False and row['checks']['replay'] is False
    assert row['error'] == {'type': kind.__name__}
    assert len(closed) == closed_count
    assert 'SENTINEL' not in json.dumps(row)


@pytest.mark.parametrize('component', ['events', 'version', 'state', 'narration', 'usage'])
def test_replay_comparison_mismatch_returns_failed_row(monkeypatch, component):
    engines = []
    real_engine = subject.TurnEngine

    class Engine(real_engine):
        def __init__(self, *args, **kwargs):
            engines.append(self)
            super().__init__(*args, **kwargs)

        async def run_turn(self, turn):
            result = await super().run_turn(turn)
            if self is not engines[-1] or len(engines) != 2:
                return result
            values = {'events': result.events, 'snapshot': result.snapshot,
                      'narration': result.narration, 'model_usage': result.model_usage}
            if component == 'events':
                values['events'] = ('synthetic',)
            elif component == 'version':
                values['snapshot'] = replace(result.snapshot, version=result.snapshot.version + 1)
            elif component == 'state':
                values['snapshot'] = replace(result.snapshot, data={'synthetic': True})
            elif component == 'narration':
                values['narration'] = 'different'
            else:
                values['model_usage'] = ('synthetic',)
            return SimpleNamespace(**values)

    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    row = _run()
    assert row['timing_status']['replay'] == 'completed'
    assert row['replay_seconds'] >= 0
    assert row['checks']['replay'] is False and row['passed'] is False
    assert row['replay_attempt_count'] == 0
    assert row['result']['window_broken'] is True
    assert 'error' not in row
