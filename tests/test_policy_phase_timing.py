"""Phase boundaries and failure evidence for the policy comparison example."""
import asyncio
import importlib
import json

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
        'completed', 'completed', 'completed', 'completed', 'not_started')
    assert row['replay_seconds'] is None
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
    assert tuple(row['timing_status'][p] for p in PHASES) == (
        'completed', 'completed', 'completed', 'completed', 'not_started')
    assert all(row[p + '_seconds'] is not None for p in PHASES[:4])
    assert row['replay_seconds'] is None
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
    assert row['timing_status']['replay'] == 'not_started'
    assert row['replay_seconds'] is None
