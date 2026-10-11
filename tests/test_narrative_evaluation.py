"""Examples exercise the real runtime; fixture prose is never ground truth."""
import asyncio
from copy import deepcopy
import json
import importlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_offline_cli_needs_no_langfuse_and_emits_json():
    code = """
import sys, runpy
class NoLangfuse:
    def find_spec(self, fullname, *args):
        if fullname == 'langfuse' or fullname.startswith('langfuse.'):
            raise ImportError('optional dependency deliberately unavailable')
sys.meta_path.insert(0, NoLangfuse())
runpy.run_module('examples.evaluation', run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['passed'] is True
    assert len(report['items']) == 2
    assert report['metadata']['model_mode'] == 'fixture'


def test_real_sqlite_cases_pass_and_are_isolated():
    from examples.evaluation.cases import CASES
    from examples.evaluation.task import task
    from examples.evaluation.checks import checks
    async def run():
        return await asyncio.gather(*(task(item=c) for c in [*CASES, *CASES]))
    outputs = asyncio.run(run())
    for case, output in zip([*CASES, *CASES], outputs):
        assert all(checks(output, case['expected_output']).values())
        assert output['replay']['new_calls'] == 0
        assert output['replay']['version_delta'] == 0
        assert output['metadata']['cost'] is None
        assert output['metadata']['usage'][0]['input_tokens'] == 0


def test_independent_checks_reject_corrupted_results():
    from examples.evaluation.cases import CASES
    from examples.evaluation.task import task
    from examples.evaluation.checks import checks
    original = asyncio.run(task(item=CASES[0]))
    corruptions = {
        'clue_retained': lambda o: o['request'].update(player_history=[]),
        'scope': lambda o: o['request'].update(foreign='FOREIGN_GAME'),
        'private_player_fields': lambda o: o['request'].update(player_history=['SELECTED_PRIVATE']),
        'state': lambda o: o.update(window_broken=False),
        'replay': lambda o: o['replay'].update(new_calls=1),
    }
    for name, corrupt in corruptions.items():
        output = deepcopy(original)
        corrupt(output)
        assert checks(output, CASES[0]['expected_output'])[name] is False


def test_local_runner_fails_on_task_and_evaluator_errors():
    from examples.evaluation.runner import run_local
    async def broken_task(**kwargs):
        raise RuntimeError('task failed')
    def broken_check(*args):
        raise RuntimeError('evaluator failed')
    assert run_local(task_fn=broken_task)['passed'] is False
    assert run_local(check_fn=broken_check)['passed'] is False
    assert run_local(check_fn=lambda *args: {})['passed'] is False


def test_cli_failed_report_has_nonzero_exit(monkeypatch, capsys):
    from examples.evaluation import __main__
    monkeypatch.setattr(__main__, 'run_local', lambda **kwargs: {'passed': False, 'items': []})
    assert __main__.main([]) == 1
    assert json.loads(capsys.readouterr().out)['passed'] is False


def test_cli_writes_local_json_and_requires_hosted_credentials(monkeypatch, tmp_path, capsys):
    from examples.evaluation import __main__
    monkeypatch.setattr(__main__, 'run_local', lambda **kwargs: {'passed': True, 'items': []})
    path = tmp_path / 'result.json'
    assert __main__.main(['--output', str(path)]) == 0
    assert json.loads(path.read_text(encoding='utf-8'))['passed'] is True
    for name in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
        monkeypatch.delenv(name, raising=False)
    assert __main__.main(['--langfuse']) == 1
    assert 'LANGFUSE_PUBLIC_KEY' in capsys.readouterr().out


def test_native_sdk_task_evaluator_and_errors_without_transport(monkeypatch):
    # CI installs the evaluation extra: this test intentionally does not skip.
    import socket
    import httpx
    Langfuse = importlib.import_module('langfuse').Langfuse
    from examples.evaluation.runner import run_langfuse
    attempts = []
    def blocked(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network forbidden in experiment tests')
    original_connect = socket.socket.connect
    def outbound_blocked(sock, address):
        # Windows asyncio creates its self-pipe via TCP loopback.
        if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return original_connect(sock, address)
        return blocked(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', outbound_blocked)
    monkeypatch.setattr(httpx.Client, 'send', blocked)
    monkeypatch.setattr(httpx.AsyncClient, 'send', blocked)
    client = Langfuse(public_key='pk-offline-narrative-test', secret_key='sk-offline',
                      tracing_enabled=False)
    # SDK scores have a separate ingestion path; suppress it before native execution.
    monkeypatch.setattr(client, 'create_score', lambda **kwargs: None)
    try:
        report = run_langfuse(client)
        assert report['passed'] is True
        assert report['experiment_id'] and report['run_name']
        async def broken_task(**kwargs):
            raise RuntimeError('task failed')
        def broken_evaluator(**kwargs):
            raise RuntimeError('evaluator failed')
        assert run_langfuse(client, task_fn=broken_task)['passed'] is False
        assert run_langfuse(client, evaluator=broken_evaluator)['passed'] is False
        assert run_langfuse(client, evaluator=lambda **kwargs: [])['passed'] is False
        Evaluation = importlib.import_module('langfuse').Evaluation
        assert run_langfuse(client, evaluator=lambda **kwargs: [
            Evaluation(name='state', value=0)])['passed'] is False
        assert attempts == []
    finally:
        client.shutdown()


def test_hosted_cli_rejects_disabled_tracing(monkeypatch, capsys):
    from examples.evaluation import __main__
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', 'pk-offline-disabled')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY', 'sk-offline-disabled')
    monkeypatch.setenv('LANGFUSE_TRACING_ENABLED', 'false')
    # Must reject before importing or constructing a hosted SDK client.
    monkeypatch.setattr(__main__, 'run_langfuse', lambda *args, **kwargs: {'passed': True})
    assert __main__.main(['--langfuse']) == 1
    assert 'LANGFUSE_TRACING_ENABLED' in capsys.readouterr().out
