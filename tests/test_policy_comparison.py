"""The offline comparison is complete, scoped, and failure-aware."""
import json


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
    async def malformed_task(*, case, policy, model_mode, model_factory, evidence_sink):
        return {'case_id': case['id'], 'policy_id': policy.value, 'passed': True,
                'checks': {'scope': False}}
    malformed = run_local(task_fn=malformed_task)
    assert malformed['passed'] is False
    assert len(malformed['rows']) == 12
    assert all(row['error']['type'] == 'ValueError' for row in malformed['rows'])
    async def unhashable_task(*, case, policy, model_mode, model_factory, evidence_sink):
        return {'case_id': case['id'], 'policy_id': policy.value, 'passed': True,
                'checks': {name: True for name in ('scope', 'dispatch_match', 'state',
                                                   'model_calls', 'recent_clue', 'old_clue_expected')},
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
