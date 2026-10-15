"""Reported-token admission through the real offline SDK transport."""
import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx2
import pytest
from agentscope.tool import ToolChoice
from openai import InternalServerError

from model_helpers import create_test_model
from test_execution_budget import Reply, completion
from storyloop_harness.generation import (
    ExecutionBudget, ExecutionTokenLimitExceeded, ExecutionUsageUnavailable,
    ExecutionRequestLimitExceeded, GenerationBudget,
    GenerationRequestLimitExceeded, execution_budget_scope,
)
from storyloop_harness.usage import collect_attempts, collect_usage
from storyloop_harness.models.attempt_tracking import AttemptTracker
from storyloop_harness.models.execution_control import admit_send
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded


@pytest.mark.parametrize('audit', [False, True])
def test_failed_fallback_and_two_models_share_tokens(audit):
    async def run():
        sent = []

        async def transport(request):
            sent.append(json.loads(request.content))
            payload = completion()
            payload['usage'] = {'prompt_tokens': 8, 'completion_tokens': 2,
                                'total_tokens': 10,
                                'prompt_tokens_details': {'cached_tokens': 6}}
            if len(sent) == 1:
                payload['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{"wrong":1}'
            return httpx2.Response(200, json=payload)

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        first = create_test_model(http_client=client, task='first')
        second = create_test_model(http_client=client, task='second')
        first.max_retries = second.max_retries = 0
        try:
            with (collect_attempts() if audit else nullcontext()) as attempts, collect_usage() as usage:
                async with execution_budget_scope(ExecutionBudget(6, 30, 30)) as control:
                    result = await first.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply)
                    assert result.content == {'prose': 'ok'}
                    result = await second([{'role': 'user', 'content': 'hi'}], structured_model=Reply)
                    assert result.metadata == {'prose': 'ok'}
                    with pytest.raises(ExecutionTokenLimitExceeded):
                        await second.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply)
                    assert control.sends == len(sent) == 3
                    assert control.token_usage.known_tokens == 30
                    assert control.token_usage.complete
                assert len(usage.records) == 2
                if audit:
                    assert [r.outcome for r in attempts.records[:3]] == ['failed', 'succeeded', 'succeeded']
            assert all(not any(name in json.dumps(body) for name in (
                'max_reported_tokens', 'token_usage', 'known_tokens', 'execution_budget',
                'generation_budget', 'AttemptTokenUsage')) for body in sent)
        finally:
            await first.client.close()
            await second.client.close()
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('input_tokens,output_tokens,limit,blocked', [
    (0, 0, 1, False), (8, 2, 10, True), (8, 2, 9, True),
])
def test_zero_equal_and_overshoot(input_tokens, output_tokens, limit, blocked):
    async def run():
        sent = []
        def transport(request):
            sent.append(json.loads(request.content))
            payload = completion()
            payload['usage'] = {'prompt_tokens': input_tokens,
                                'completion_tokens': output_tokens,
                                'total_tokens': input_tokens + output_tokens}
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        model.max_retries = 0
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30, limit)) as control:
                await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                       tool_choice=ToolChoice(mode='auto'))
                assert control.token_usage.known_tokens == input_tokens + output_tokens
                assert control.token_usage.complete
                if blocked:
                    with pytest.raises(ExecutionTokenLimitExceeded):
                        await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                               tool_choice=ToolChoice(mode='auto'))
                    assert len(sent) == 1
                else:
                    await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                           tool_choice=ToolChoice(mode='auto'))
                    assert len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('audit', [False, True])
@pytest.mark.parametrize('usage', [None, {'prompt_tokens': -1, 'completion_tokens': 2, 'total_tokens': 1}])
def test_unavailable_usage_blocks_later_send(audit, usage):
    async def run():
        sent = []
        def transport(request):
            sent.append(request)
            payload = completion()
            if usage is not None:
                payload['usage'] = usage
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        model.max_retries = 0
        try:
            with (collect_attempts() if audit else nullcontext()) as attempts:
                async with execution_budget_scope(ExecutionBudget(6, 30, 100)) as control:
                    await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                           tool_choice=ToolChoice(mode='auto'))
                    with pytest.raises(ExecutionUsageUnavailable):
                        await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                               tool_choice=ToolChoice(mode='auto'))
                    assert len(sent) == control.sends == 1
                    assert not control.token_usage.complete
                    assert control.token_usage.known_tokens == 0
                if audit:
                    assert attempts.records[0].usage_state in ('missing', 'invalid')
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('retry', [False, True])
def test_failed_http_send_claims_usage_unknown(retry):
    async def run():
        sent = []
        def unavailable(request):
            sent.append(request)
            return httpx2.Response(503, headers={'retry-after-ms': '0'},
                                   json={'error': {'message': 'offline', 'type': 'server_error'}})
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(unavailable))
        model = create_test_model(http_client=client, sdk_retries=2)
        model.max_retries = 2 if retry else 0
        model.retry_delay = 0
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30, 100)) as control:
                with pytest.raises((ExecutionUsageUnavailable if retry else InternalServerError)):
                    await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                           tool_choice=ToolChoice(mode='auto'))
                with pytest.raises(ExecutionUsageUnavailable):
                    await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                           tool_choice=ToolChoice(mode='auto'))
                assert len(sent) == control.sends == 1
                assert not control.token_usage.complete
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_invalid_final_snapshot_keeps_private_lower_bound():
    async def run():
        tracker = AttemptTracker('m', 't')
        async with execution_budget_scope(ExecutionBudget(6, 30, 100)) as control:
            with collect_attempts() as audit, tracker.invocation():
                async with tracker.attempt():
                    admit_send(control, None, tracker.current_token_attempt())
                    tracker.observe(SimpleNamespace(usage=SimpleNamespace(
                        input_tokens=5, output_tokens=1, cache_input_tokens=0)))
                    tracker.observe(SimpleNamespace(usage=SimpleNamespace(
                        input_tokens=-1, output_tokens=1, cache_input_tokens=0)))
            assert control.token_usage.known_tokens == 6
            assert not control.token_usage.complete
            assert audit.records[0].usage is None
            assert audit.records[0].usage_state == 'invalid'
    asyncio.run(run())


def test_direct_and_nested_ordinary_calls_cannot_borrow_receipt():
    async def run():
        sent = []
        def transport(request):
            sent.append(request)
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30, 100)) as control:
                with pytest.raises(ValueError, match='budget-critical'):
                    await model.client.chat.completions.create(
                        model='test-model', messages=[{'role': 'user', 'content': 'hi'}],
                        extra_body={'messages': []})
                with pytest.raises(ExecutionUsageUnavailable):
                    await model.client.chat.completions.create(
                        model='test-model', messages=[{'role': 'user', 'content': 'hi'}])
                with model._attempt_tracker.invocation():
                    async with model._attempt_tracker.attempt():
                        with pytest.raises(ExecutionUsageUnavailable):
                            await model([{'role': 'user', 'content': 'hi'}])
                assert control.sends == len(sent) == 0
                assert control.token_usage.complete
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_two_real_guarded_sends_in_one_attempt_mark_coverage_incomplete():
    async def run():
        sent = []
        def transport(request):
            sent.append(json.loads(request.content))
            payload = completion()
            payload['usage'] = {'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10}
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30, 100)) as control:
                with model._attempt_tracker.invocation():
                    async with model._attempt_tracker.attempt():
                        for _ in range(2):
                            response = await model.client.chat.completions.create(
                                model='test-model', messages=[{'role': 'user', 'content': 'hi'}])
                        model._attempt_tracker.observe(SimpleNamespace(usage=SimpleNamespace(
                            input_tokens=response.usage.prompt_tokens,
                            output_tokens=response.usage.completion_tokens,
                            cache_input_tokens=0,
                        )))
                assert len(sent) == control.sends == 2
                assert control.token_usage.known_tokens == 10
                assert not control.token_usage.complete
                with pytest.raises(ExecutionUsageUnavailable):
                    await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply)
                assert len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('limit', [10, 11, None])
def test_reentrant_other_model_waits_for_outer_attempt_settlement(limit):
    async def run():
        posts = []
        async def transport(request):
            posts.append(request)
            if len(posts) == 1:
                if limit is None:
                    nested = await second.generate_structured_output(
                        [{'role': 'user', 'content': 'nested'}], Reply,
                        tool_choice=ToolChoice(mode='auto'))
                    assert nested.content == {'prose': 'ok'}
                    assert len(posts) == control.sends == 2
                else:
                    with pytest.raises(ExecutionUsageUnavailable):
                        await second.generate_structured_output(
                            [{'role': 'user', 'content': 'nested'}], Reply,
                            tool_choice=ToolChoice(mode='auto'))
                    assert len(posts) == control.sends == 1
                    assert control.token_usage.known_tokens == 0
                    assert control.token_usage.complete
            payload = completion()
            payload['usage'] = {'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10}
            return httpx2.Response(200, json=payload)

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        first = create_test_model(http_client=client, task='first', sdk_retries=0)
        second = create_test_model(http_client=client, task='second', sdk_retries=0)
        first.max_retries = second.max_retries = 0
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30, limit)) as control:
                result = await first.generate_structured_output(
                    [{'role': 'user', 'content': 'outer'}], Reply,
                    tool_choice=ToolChoice(mode='auto'))
                assert result.content == {'prose': 'ok'}
                assert len(posts) == control.sends == (2 if limit is None else 1)
                if limit is not None:
                    assert control.token_usage.known_tokens == 10
                    assert control.token_usage.complete
                if limit == 10:
                    with pytest.raises(ExecutionTokenLimitExceeded):
                        await second.generate_structured_output(
                            [{'role': 'user', 'content': 'after'}], Reply,
                            tool_choice=ToolChoice(mode='auto'))
                    assert len(posts) == control.sends == 1
                else:
                    await second.generate_structured_output(
                        [{'role': 'user', 'content': 'after'}], Reply,
                        tool_choice=ToolChoice(mode='auto'))
                    assert len(posts) == control.sends == (3 if limit is None else 2)
        finally:
            await first.client.close()
            await second.client.close()
            await client.aclose()

    asyncio.run(run())


def test_local_rejections_leave_token_ledger_available():
    async def run():
        sent = []
        def transport(request):
            sent.append(json.loads(request.content))
            payload = completion()
            payload['usage'] = {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4}
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0,
                                  generation_budget=GenerationBudget(1, 30))
        model.max_retries = 0
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30, 100)) as control:
                with pytest.raises(ContextBudgetExceeded):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'too large' * 2000}], Reply,
                        context_budget=ContextBudget(context_window_tokens=1500,
                                                     output_tokens=100, safety_margin_tokens=0))
                with pytest.raises(Exception):
                    await model.generate_structured_output([], Reply)
                assert len(sent) == control.sends == 0
                assert control.token_usage.complete
                await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply,
                                                       tool_choice=ToolChoice(mode='auto'),
                                                       context_budget=ContextBudget(
                                                           context_window_tokens=10000,
                                                           output_tokens=100,
                                                           safety_margin_tokens=0))
                assert control.token_usage.known_tokens == 4
                assert control.token_usage.complete
                assert sent[0]['max_completion_tokens'] == 100
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_generation_request_rejection_does_not_poison_token_coverage():
    async def run():
        sent = []
        def transport(request):
            sent.append(request)
            payload = completion()
            payload['usage'] = {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}
            if len(sent) == 1:
                payload['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{"wrong":1}'
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0,
                                  generation_budget=GenerationBudget(1, 30))
        model.max_retries = 0
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30, 100)) as control:
                with pytest.raises(GenerationRequestLimitExceeded):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'bad'}], Reply)
                assert len(sent) == control.sends == 1
                assert control.token_usage.known_tokens == 6
                assert control.token_usage.complete
                await model.generate_structured_output(
                    [{'role': 'user', 'content': 'good'}], Reply,
                    tool_choice=ToolChoice(mode='auto'))
                assert len(sent) == control.sends == 2
                assert control.token_usage.known_tokens == 12
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_nested_matching_scope_fresh_scope_and_child_ownership():
    async def run():
        sent = []
        def transport(request):
            sent.append(request)
            payload = completion()
            payload['usage'] = {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        budget = ExecutionBudget(6, 30, 6)
        try:
            async with execution_budget_scope(budget) as outer:
                async with execution_budget_scope(budget) as inner:
                    assert inner is outer
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'hi'}], Reply,
                        tool_choice=ToolChoice(mode='auto'))
                with pytest.raises(ValueError):
                    async with execution_budget_scope(ExecutionBudget(6, 30, 7)):
                        pass
                async def child():
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'hi'}], Reply)
                with pytest.raises(ExecutionRequestLimitExceeded):
                    await asyncio.create_task(child())
                with pytest.raises(ExecutionTokenLimitExceeded):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'hi'}], Reply)
                assert outer.sends == 1
            async with execution_budget_scope(budget) as fresh:
                assert fresh is not outer
                await model.generate_structured_output(
                    [{'role': 'user', 'content': 'hi'}], Reply,
                    tool_choice=ToolChoice(mode='auto'))
                assert fresh.token_usage.known_tokens == 6
            assert len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_concurrent_top_level_scopes_on_one_model_are_independent():
    async def run():
        sent = []
        async def transport(request):
            sent.append(request)
            await asyncio.sleep(0)
            payload = completion()
            payload['usage'] = {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        async def turn():
            async with execution_budget_scope(ExecutionBudget(1, 30, 3)) as control:
                await model.generate_structured_output(
                    [{'role': 'user', 'content': 'hi'}], Reply,
                    tool_choice=ToolChoice(mode='auto'))
                assert control.sends == 1
                assert control.token_usage.known_tokens == 3
                assert control.token_usage.complete
        try:
            await asyncio.gather(turn(), turn())
            assert len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_ordinary_call_unchanged_when_token_policy_disabled():
    async def run():
        sent = []
        def transport(request):
            sent.append(request)
            payload = completion()
            payload['usage'] = {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30)) as control:
                await model([{'role': 'user', 'content': 'hi'}])
                await model.client.chat.completions.create(
                    model='test-model', messages=[{'role': 'user', 'content': 'hi'}])
                assert control.sends == len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())
