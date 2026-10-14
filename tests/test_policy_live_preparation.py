"""Selection and evidence guarantees for the controlled comparison."""
import pytest
import asyncio
import json
import httpx2
from model_helpers import create_test_model
from storyloop_harness.generation import GenerationBudget


def _completion(arguments, usage=None):
    return {'id': 'offline', 'created': 0, 'model': 'offline-test', 'object': 'chat.completion',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'tool', 'type': 'function',
                    'function': {'name': 'generate_structured_output', 'arguments': arguments}}]}}],
            'usage': usage}


def _native_factory(transport):
    def factory():
        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        return create_test_model(model='offline-test', key='CREDENTIAL_SENTINEL',
                                 base_url='https://URL_SENTINEL.invalid/v1',
                                 http_client=http_client, sdk_retries=2, task='scene_turn',
                                 generation_budget=GenerationBudget(3, 90))
    return factory


def test_native_sdk_fallback_attempt_totals_and_privacy():
    from examples.policy_comparison.runner import run_local
    wire = []

    def transport(request):
        wire.append(json.loads(request.content))
        arguments = ('invalid-json' if len(wire) == 1 else
                     json.dumps({'prose': 'window breaks', 'interaction': 'action',
                                 'action_id': 'break_shop_window', 'action': {'status': 'occurred'}}))
        return httpx2.Response(200, json=_completion(arguments, {'prompt_tokens': 10 * len(wire),
            'completion_tokens': len(wire), 'total_tokens': 11 * len(wire)}))

    report = run_local(case_ids=('old-clue',), policy_ids=('history-recent-v1',),
                       model_mode='sdk_mock', model_factory=_native_factory(transport))
    row = report['rows'][0]
    assert report['coverage']['full_matrix_selected'] is False
    assert row['passed'] is True
    assert row['checks']['same_authoritative_result'] is None
    assert row['provider_input_tokens'] == 30
    assert row['provider_output_tokens'] == 3
    assert [a['outcome'] for a in row['attempt_summary']['attempts']] == ['failed', 'succeeded']
    assert row['attempt_summary']['usage_complete'] is True
    assert row['metadata']['generation_budget'] == {'max_requests': 3, 'timeout_seconds': 90}
    assert row['generation_seconds'] >= 0
    assert wire[0]['max_completion_tokens'] == row['metadata']['budget']['output_tokens']
    assert wire[0]['temperature'] == 0.7
    assert row['cost'] is None
    encoded = json.dumps(report)
    assert 'CREDENTIAL_SENTINEL' not in encoded and 'URL_SENTINEL' not in encoded
    assert 'window breaks' not in encoded


def test_native_full_policy_group_does_not_apply_fixture_equality_gate():
    from examples.policy_comparison.runner import run_local
    counter = 0

    def transport(request):
        nonlocal counter
        counter += 1
        reply = {'prose': f'variant {counter}', 'interaction': 'action',
                 'action_id': 'break_shop_window', 'action': {'status': 'occurred'}}
        return httpx2.Response(200, json=_completion(json.dumps(reply),
            {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}))

    report = run_local(case_ids=('old-clue',), model_mode='sdk_mock',
                       model_factory=_native_factory(transport))
    assert len(report['rows']) == 4
    assert report['passed'] is True
    assert all(row['checks']['same_authoritative_result'] is None for row in report['rows'])
    assert counter == 4


def test_native_sdk_missing_usage_is_null_and_failure_is_safe():
    from examples.policy_comparison.runner import run_local

    def transport(request):
        return httpx2.Response(200, json=_completion('invalid-json', None))

    report = run_local(case_ids=('old-clue',), policy_ids=('history-recent-v1',),
                       model_mode='sdk_mock', model_factory=_native_factory(transport))
    row = report['rows'][0]
    assert report['passed'] is False
    assert row['provider_input_tokens'] is None
    assert row['attempt_summary']['missing_attempts'] > 0
    assert row['turn_seconds'] >= 0
    assert row['generation_seconds'] >= 0
    assert row['error']['type']
    assert 'URL_SENTINEL' not in json.dumps(report)


def test_cancellation_preserves_partial_report_and_original_identity(tmp_path):
    from examples.policy_comparison.runner import run_local
    original = asyncio.CancelledError('CANCEL_SENTINEL')
    output = tmp_path / 'partial.json'

    async def cancelled(*, case, policy, model_mode, model_factory, evidence_sink):
        evidence_sink({'case_id': case['id'], 'policy_id': policy.value,
                       'passed': False, 'error': {'type': 'CancelledError'},
                       'attempt_summary': {'attempts': []}, 'cost': None})
        raise original

    with pytest.raises(asyncio.CancelledError) as caught:
        run_local(task_fn=cancelled, case_ids=('old-clue',), output_path=output)
    assert caught.value is original
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'cancelled'
    assert report['passed'] is False
    assert len(report['coverage']['selected_pairs']) == 4
    assert len(report['coverage']['completed_pairs']) == 1
    assert len(report['rows']) == 1
    assert 'CANCEL_SENTINEL' not in json.dumps(report)
    with pytest.raises(asyncio.CancelledError) as caught_again:
        run_local(task_fn=cancelled, case_ids=('old-clue',), output_path=tmp_path)
    assert caught_again.value is original


def test_native_cancellation_keeps_attempt_and_closes_model():
    from examples.policy_comparison.task import task
    from examples.policy_comparison.cases import CASES
    from storyloop_harness.context import ContextPolicy
    entered = asyncio.Event()
    blocker = asyncio.Event()
    captured = []
    models = []

    async def transport(request):
        entered.set()
        await blocker.wait()
        return httpx2.Response(200, json=_completion('invalid-json'))

    def factory():
        model = _native_factory(transport)()
        models.append(model)
        return model

    async def run():
        running = asyncio.create_task(task(case=CASES[0], policy=ContextPolicy.DEFAULT,
                                           model_mode='sdk_mock', model_factory=factory,
                                           evidence_sink=captured.append))
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(run())
    assert len(captured) == 1
    row = captured[0]
    assert row['passed'] is False
    assert row['error']['type'] == 'CancelledError'
    assert row['attempt_summary']['attempts'][0]['outcome'] == 'cancelled'
    assert row['turn_seconds'] >= 0
    assert models[0].client.is_closed


def test_stream_cancellation_retains_last_known_usage():
    from examples.policy_comparison.task import task
    from examples.policy_comparison.cases import CASES
    from storyloop_harness.context import ContextPolicy
    entered = asyncio.Event()
    blocker = asyncio.Event()
    captured = []

    def sse(payload):
        return ('data: ' + json.dumps(payload) + '\n\n').encode()

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield sse({'id': 'offline', 'created': 0, 'model': 'offline-test',
                       'object': 'chat.completion.chunk', 'choices': [],
                       'usage': {'prompt_tokens': 7, 'completion_tokens': 2,
                                 'total_tokens': 9}})
            entered.set()
            await blocker.wait()

        async def aclose(self):
            pass

    def factory():
        model = _native_factory(lambda request: httpx2.Response(
            200, headers={'content-type': 'text/event-stream'}, stream=Stream()))()
        model.stream = True
        return model

    async def run():
        running = asyncio.create_task(task(case=CASES[0], policy=ContextPolicy.DEFAULT,
                                           model_mode='sdk_mock', model_factory=factory,
                                           evidence_sink=captured.append))
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(run())
    row = captured[0]
    assert row['provider_input_tokens'] == 7
    assert row['provider_output_tokens'] == 2
    assert row['attempt_summary']['incomplete_stream_attempts'] == 1
    assert row['attempt_summary']['usage_complete'] is False


def test_native_deadline_is_failed_row_with_no_invented_usage():
    from examples.policy_comparison.runner import run_local

    async def transport(request):
        await asyncio.sleep(0.1)
        return httpx2.Response(200, json=_completion('invalid-json'))

    def factory():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        return create_test_model(http_client=client, sdk_retries=2, task='scene_turn',
                                 generation_budget=GenerationBudget(3, 0.01))

    report = run_local(case_ids=('old-clue',), policy_ids=('history-recent-v1',),
                       model_mode='sdk_mock', model_factory=factory)
    row = report['rows'][0]
    assert report['passed'] is False
    assert row['error']['type'] == 'GenerationTimeout'
    assert row['provider_input_tokens'] is None
    assert row['attempt_summary']['usage_complete'] is False


def test_native_invalid_usage_is_not_coerced_to_zero():
    from examples.policy_comparison.runner import run_local

    def transport(request):
        reply = {'prose': 'window breaks', 'interaction': 'action',
                 'action_id': 'break_shop_window', 'action': {'status': 'occurred'}}
        return httpx2.Response(200, json=_completion(json.dumps(reply),
            {'prompt_tokens': -1, 'completion_tokens': 2, 'total_tokens': 1}))

    report = run_local(case_ids=('old-clue',), policy_ids=('history-recent-v1',),
                       model_mode='sdk_mock', model_factory=_native_factory(transport))
    row = report['rows'][0]
    assert row['provider_input_tokens'] is None
    assert row['attempt_summary']['invalid_attempts'] == 1
    assert row['attempt_summary']['usage_complete'] is False


def test_selected_coverage_and_fixture_gates():
    from examples.policy_comparison.runner import run_local

    for cases, policies, count in ((None, None, 12), (('old-clue',), None, 4),
                                   (None, ('history-recent-v1',), 3),
                                   (('old-clue',), ('history-recent-v1',), 1)):
        report = run_local(case_ids=cases, policy_ids=policies)
        assert len(report['rows']) == count
        assert report['coverage']['dataset_pairs'] == 12
        assert len(report['coverage']['selected_pairs']) == count
        assert len(report['coverage']['completed_pairs']) == count
        assert len(report['coverage']['excluded_pairs']) == 12 - count
        assert report['coverage']['full_matrix_selected'] is (count == 12)
        assert all(row['checks']['same_authoritative_result'] is (True if count in (12, 4) else None)
                   for row in report['rows'])
        assert all(row['provider_input_tokens'] is None and
                   row['attempt_summary']['attempts'] == [] and
                   row['attempt_summary']['usage_complete'] is False and row['cost'] is None
                   for row in report['rows'])


def test_invalid_selection_and_live_scope_rejected_before_model():
    from examples.policy_comparison.runner import run_local

    def forbidden():
        raise AssertionError('model constructed')

    for options in ({'case_ids': ()}, {'case_ids': ('missing',)},
                    {'case_ids': ('old-clue', 'old-clue')}, {'policy_ids': ()},
                    {'policy_ids': ('missing',)},
                    {'model_mode': 'live'},
                    {'model_mode': 'live', 'case_ids': ('old-clue',)}):
        with pytest.raises(ValueError):
            run_local(model_factory=forbidden, **options)


def test_cli_live_requires_exact_explicit_pair_before_runner(monkeypatch):
    from examples.policy_comparison import __main__

    def forbidden(**kwargs):
        raise AssertionError('runner called')

    monkeypatch.setattr(__main__, 'run_local', forbidden)
    for arguments in (['--live'], ['--live', '--case', 'old-clue'],
                      ['--live', '--case', 'old-clue', '--policy', 'history-recent-v1',
                       '--policy', 'history-summary-v1']):
        with pytest.raises(SystemExit) as caught:
            __main__.main(arguments)
        assert caught.value.code == 2


def test_live_model_forwards_only_optional_generation_budget(monkeypatch):
    from examples.persistent_story import live_model
    budget = GenerationBudget(3, 90)
    monkeypatch.setenv('STORY_MODEL_API_KEY', 'SECRET_SENTINEL')
    monkeypatch.setenv('STORY_MODEL_NAME', 'offline-test')
    monkeypatch.setenv('STORY_MODEL_BASE_URL', 'https://URL_SENTINEL.invalid/v1')
    model = live_model(generation_budget=budget)
    try:
        assert model.generation_budget is budget
        assert model.stream is False
    finally:
        asyncio.run(model.client.close())
