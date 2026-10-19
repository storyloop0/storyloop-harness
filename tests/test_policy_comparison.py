"""The offline comparison is complete, scoped, and failure-aware."""
import asyncio
from copy import deepcopy
import importlib
import json
import pytest


def test_full_offline_matrix_records_comparable_evidence(monkeypatch):
    import socket
    original_connect = socket.socket.connect
    def outbound_blocked(sock, address):
        if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return original_connect(sock, address)
        raise AssertionError('offline comparison attempted a network call')
    monkeypatch.setattr(socket.socket, 'connect', outbound_blocked)
    from examples.policy_comparison.runner import run_local
    report = run_local()
    assert report['passed'] is True
    assert report['status'] == 'offline_fixture'
    assert len(report['rows']) == 12
    assert len({(row['case_id'], row['policy_id']) for row in report['rows']}) == 12
    assert {row['case_id'] for row in report['rows']} == {'old-clue', 'recent-clue', 'unrelated'}
    for row in report['rows']:
        assert row['passed'] is True
        assert row['provider_input_tokens'] is None
        assert row['provider_output_tokens'] is None
        assert row['cost'] is None
        assert row['estimated_request_tokens'] > 0
        assert row['projection_seconds'] >= 0
        assert row['turn_seconds'] >= 0
        assert all(row[f'{phase}_seconds'] >= 0 for phase in
                   ('preparation', 'projection', 'generation', 'turn', 'replay'))
        assert all(row['timing_status'][phase] == 'completed' for phase in
                   ('preparation', 'projection', 'generation', 'turn', 'replay'))
        assert row['replay'] == {'new_calls': 0, 'new_events': 0, 'version_delta': 0,
                                 'same_state': True, 'same_narration': True, 'usage_count': 0}
        assert row['replay_attempt_count'] == 0
        assert row['checks']['replay'] is True
        assert row['metadata']['timing_version'] == 'policy-phases-v1'
        assert row['source_ids']['selected']
        assert row['source_ids']['omitted']
        assert row['checks']['scope'] is True
        assert row['checks']['dispatch_match'] is True
        assert row['checks']['state'] is True
        assert row['checks']['model_calls'] is True
        assert row['metadata']['dataset_id']
        assert row['metadata']['scenario_id']
        assert row['metadata']['harness_version']
        assert row['metadata']['fixture_id']
        assert len(row['metadata']['prompt_sha256']) == 64
        assert len(row['metadata']['schema_sha256']) == 64
    old = {row['policy_id']: row for row in report['rows'] if row['case_id'] == 'old-clue'}
    assert 'player-0' not in old['history-recent-v1']['source_ids']['selected']
    assert 'player-0' in old['history-relevant-v1']['source_ids']['selected']
    assert 'player:16' in old['history-summary-v1']['source_ids']['selected']
    assert all(row['checks']['recent_clue'] for row in report['rows'])
    assert len({json.dumps(row['result'], sort_keys=True) for row in old.values()}) == 1
    encoded = json.dumps(report)
    assert 'FOREIGN_GAME' not in encoded and 'UNSELECTED_PRIVATE' not in encoded
    assert 'brasskey731 old clue' not in encoded


def test_task_failure_and_missing_row_make_runner_and_cli_fail(monkeypatch, capsys):
    from examples.policy_comparison import __main__
    from examples.policy_comparison.runner import run_local
    async def broken_task(**kwargs):
        raise RuntimeError('synthetic failure')
    report = run_local(task_fn=broken_task)
    assert report['passed'] is False
    assert len(report['rows']) == 12
    assert all(row['error']['type'] == 'RuntimeError' for row in report['rows'])
    async def malformed_task(*, case, policy, model_mode, model_factory, evidence_sink, pricing):
        return {'case_id': case['id'], 'policy_id': policy.value, 'passed': True,
                'checks': {'scope': False}}
    malformed = run_local(task_fn=malformed_task)
    assert malformed['passed'] is False
    assert len(malformed['rows']) == 12
    assert all(row['error']['type'] == 'ValueError' for row in malformed['rows'])
    async def unhashable_task(*, case, policy, model_mode, model_factory, evidence_sink, pricing):
        return {'case_id': case['id'], 'policy_id': policy.value, 'passed': True,
                'checks': {name: True for name in ('scope', 'dispatch_match', 'state',
                                                   'model_calls', 'recent_clue', 'old_clue_expected',
                                                   'replay')},
                'result': {'window_broken': True, 'action_outcome': 'accepted',
                           'state_version_delta': 1, 'state_sha256': [], 'events_sha256': 'a' * 64}}
    unhashable = run_local(task_fn=unhashable_task)
    assert unhashable['passed'] is False
    assert len(unhashable['rows']) == 12
    monkeypatch.setattr(__main__, 'run_local', lambda **kwargs: {'passed': False, 'rows': report['rows'][:1]})
    assert __main__.main([]) == 1
    assert json.loads(capsys.readouterr().out)['passed'] is False


def test_cli_writes_same_json_and_returns_zero(tmp_path, monkeypatch, capsys):
    from examples.policy_comparison import __main__
    target = tmp_path / 'comparison.json'
    assert __main__.main(['--case', 'old-clue', '--policy', 'history-recent-v1',
                          '--output', str(target)]) == 0
    assert json.loads(target.read_text(encoding='utf-8')) == json.loads(capsys.readouterr().out)


def test_git_revision_is_optional(monkeypatch):
    import subprocess
    from examples.policy_comparison import task
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(task.subprocess, 'run', unavailable)
    assert task._revision() is None
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('git', 5)
    monkeypatch.setattr(task.subprocess, 'run', timeout)
    assert task._revision() is None


@pytest.mark.parametrize('mutation,expected_error', [
    ('missing', 'ValueError'), ('nonbool', 'ValueError'), ('false', None),
])
def test_runner_requires_exact_boolean_replay_check(mutation, expected_error):
    from examples.policy_comparison.runner import run_local
    baseline = run_local(case_ids=('old-clue',), policy_ids=('history-recent-v1',))['rows'][0]

    async def altered(*, case, policy, model_mode, model_factory, evidence_sink, pricing):
        row = deepcopy(baseline)
        row['checks'].pop('same_authoritative_result', None)
        if mutation == 'missing':
            del row['checks']['replay']
        else:
            row['checks']['replay'] = 1 if mutation == 'nonbool' else False
        if mutation == 'false':
            row['passed'] = False
            row['acceptance']['runtime_contract'] = 'failed'
        return row

    report = run_local(task_fn=altered, case_ids=('old-clue',),
                       policy_ids=('history-recent-v1',))
    assert report['passed'] is False and len(report['rows']) == 1
    if expected_error:
        assert report['rows'][0]['error'] == {'type': expected_error}
    else:
        assert report['rows'][0]['checks']['replay'] is False
        assert report['rows'][0]['passed'] is False


def test_actual_replay_failure_propagates_to_runner_and_cli(monkeypatch, capsys):
    from examples.policy_comparison import __main__
    from examples.policy_comparison.runner import run_local
    subject = importlib.import_module('examples.policy_comparison.task')
    real_engine = subject.TurnEngine
    engines = []
    calls = []

    class Delegate(subject.Fixture):
        async def __call__(self, messages, **kwargs):
            calls.append(1)
            return await super().__call__(messages, **kwargs)

    class Engine(real_engine):
        def __init__(self, store, package, model, **kwargs):
            engines.append(self)
            self.replay_model = model
            super().__init__(store, package, model, **kwargs)

        async def run_turn(self, turn):
            if len(engines) == 2 and self is engines[1]:
                await self.replay_model([], injected=True)
            return await super().run_turn(turn)

    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    report = run_local(case_ids=('old-clue',), policy_ids=('history-recent-v1',),
                       model_factory=Delegate)
    assert report['passed'] is False and len(report['rows']) == 1
    row = report['rows'][0]
    assert len(calls) == 1
    assert row['result']['window_broken'] is True
    assert row['timing_status']['replay'] == 'failed'
    assert row['checks']['replay'] is False
    assert row['replay']['new_calls'] == 1
    monkeypatch.setattr(__main__, 'run_local', lambda **kwargs: report)
    assert __main__.main([]) == 1
    assert json.loads(capsys.readouterr().out)['rows'][0]['checks']['replay'] is False


def test_actual_replay_cancellation_writes_partial_report_and_cli_returns_130(
        monkeypatch, tmp_path):
    from examples.policy_comparison import __main__
    from examples.policy_comparison.runner import run_local
    subject = importlib.import_module('examples.policy_comparison.task')
    real_engine = subject.TurnEngine
    original = asyncio.CancelledError('PRIVATE_SENTINEL')
    engines = []
    output = tmp_path / 'cancelled.json'

    class Engine(real_engine):
        def __init__(self, *args, **kwargs):
            engines.append(self)
            super().__init__(*args, **kwargs)

        async def run_turn(self, turn):
            if len(engines) == 2 and self is engines[1]:
                raise original
            return await super().run_turn(turn)

    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    with pytest.raises(asyncio.CancelledError) as caught:
        run_local(case_ids=('old-clue',), policy_ids=('history-recent-v1',),
                  output_path=output)
    assert caught.value is original
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'cancelled' and report['passed'] is False
    assert len(report['rows']) == len(report['coverage']['completed_pairs']) == 1
    row = report['rows'][0]
    assert all(row['timing_status'][phase] == 'completed' for phase in
               ('preparation', 'projection', 'generation', 'turn'))
    assert row['timing_status']['replay'] == 'cancelled'
    assert 'PRIVATE_SENTINEL' not in json.dumps(report)
    monkeypatch.setattr(__main__, 'run_local', lambda **kwargs: (_ for _ in ()).throw(original))
    assert __main__.main([]) == 130


@pytest.mark.parametrize('interruption', ['exception', 'cancel'])
def test_emitted_forgery_cannot_claim_success_or_replace_selected_pair(
        tmp_path, interruption):
    from examples.policy_comparison.runner import run_local
    baseline = run_local(case_ids=('old-clue',),
                         policy_ids=('history-recent-v1',))['rows'][0]
    output = tmp_path / 'report.json'
    original = asyncio.CancelledError('PRIVATE_CANCEL')

    async def forged(*, evidence_sink, **kwargs):
        row = deepcopy(baseline)
        row['case_id'] = 'forged-case'
        row['policy_id'] = 'forged-policy'
        row['passed'] = True
        row['acceptance']['semantic_quality'] = 'passed'
        row['result']['state_sha256'] = 'forged-result'
        evidence_sink(row)
        if interruption == 'cancel':
            raise original
        raise RuntimeError('PRIVATE_FAILURE')

    if interruption == 'cancel':
        with pytest.raises(asyncio.CancelledError) as caught:
            run_local(task_fn=forged, case_ids=('old-clue',),
                      policy_ids=('history-recent-v1',), output_path=output)
        assert caught.value is original
        report = json.loads(output.read_text(encoding='utf-8'))
        assert report['status'] == 'cancelled'
    else:
        report = run_local(task_fn=forged, case_ids=('old-clue',),
                           policy_ids=('history-recent-v1',), output_path=output)
    assert report['passed'] is False
    assert report['coverage']['completed_pairs'] == [
        {'case_id': 'old-clue', 'policy_id': 'history-recent-v1'}]
    row = report['rows'][0]
    assert row['case_id'] == 'old-clue' and row['policy_id'] == 'history-recent-v1'
    assert row['passed'] is False
    assert row['acceptance']['runtime_contract'] == 'failed'
    assert row['acceptance']['structural_content'] == 'unavailable'
    assert row['acceptance']['bounded_duplicate_check'] == 'unavailable'
    assert row['acceptance']['semantic_quality'] == 'unreviewed'
    assert 'result' not in row
    assert 'forged-result' not in json.dumps(report)


def test_emitted_complete_row_after_failure_keeps_validated_result_but_fails_runtime():
    from examples.policy_comparison.runner import run_local
    baseline = run_local(case_ids=('old-clue',),
                         policy_ids=('history-recent-v1',))['rows'][0]

    async def failed_after_turn(*, evidence_sink, **kwargs):
        row = deepcopy(baseline)
        row['checks'].pop('same_authoritative_result', None)
        row['acceptance']['semantic_quality'] = 'passed'
        evidence_sink(row)
        raise RuntimeError('PRIVATE_CLEANUP')

    report = run_local(task_fn=failed_after_turn, case_ids=('old-clue',),
                       policy_ids=('history-recent-v1',))
    row = report['rows'][0]
    assert report['passed'] is row['passed'] is False
    assert row['result'] == baseline['result']
    assert row['acceptance']['runtime_contract'] == 'failed'
    assert row['acceptance']['structural_content'] == 'passed'
    assert row['acceptance']['bounded_duplicate_check'] == 'passed'
    assert row['acceptance']['semantic_quality'] == 'unreviewed'


def test_malformed_emitted_metadata_does_not_break_failure_report(tmp_path):
    from examples.policy_comparison.runner import run_local

    async def malformed(*, case, policy, evidence_sink, **kwargs):
        evidence_sink({'case_id': case['id'], 'policy_id': policy.value,
                       'metadata': {'invalid': object()}, 'passed': True})
        raise RuntimeError('PRIVATE_FAILURE')

    output = tmp_path / 'report.json'
    report = run_local(task_fn=malformed, case_ids=('old-clue',),
                       policy_ids=('history-recent-v1',), output_path=output)
    assert report == json.loads(output.read_text(encoding='utf-8'))
    assert report['passed'] is report['rows'][0]['passed'] is False
    assert report['rows'][0]['metadata'] == {'pricing': None}
