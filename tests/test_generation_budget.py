"""Structured generation send allowance through the real OpenAI SDK transport."""
import asyncio
import json
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import httpx2
import pytest
from agentscope.tool import ToolChoice
from openai import InternalServerError
from pydantic import BaseModel

from model_helpers import create_test_model
from storyloop_harness.generation import GenerationBudget, GenerationRequestLimitExceeded
from storyloop_harness.usage import collect_attempts, collect_usage
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded


class Reply(BaseModel):
    prose: str


def completion(arguments='{"prose":"ok"}', *, usage=True):
    return {
        'id': 'offline', 'created': 0, 'model': 'offline-test', 'object': 'chat.completion',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'tool', 'type': 'function',
                'function': {'name': 'generate_structured_output', 'arguments': arguments}}],
        }}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5} if usage else None,
    }


def test_public_policy_validation_and_immutability():
    assert GenerationBudget() == GenerationBudget(3, 90)
    for requests in (0, -1, True, 1.5, '3'):
        with pytest.raises((TypeError, ValueError)):
            GenerationBudget(max_requests=requests)
    for seconds in (0, -1, True, '90', float('nan'), float('inf')):
        with pytest.raises((TypeError, ValueError)):
            GenerationBudget(timeout_seconds=seconds)
    with pytest.raises(FrozenInstanceError):
        GenerationBudget().max_requests = 4
    with pytest.raises(TypeError, match='generation_budget must be GenerationBudget'):
        create_test_model(generation_budget={'max_requests': 3})


def test_budget_blocks_fourth_strategy_before_sdk_send():
    async def run():
        sent = []

        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion('invalid-json'))

        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=http_client, sdk_retries=2,
                                  generation_budget=GenerationBudget(3, 90))
        model.max_retries = 0
        model._get_disable_thinking_kwargs = lambda: {'extra_body': {'thinking': {'type': 'disabled'}}}
        try:
            with collect_attempts() as attempts:
                with pytest.raises(GenerationRequestLimitExceeded, match='^Structured generation request limit exceeded$'):
                    await model.generate_structured_output([{'role': 'user', 'content': 'hello'}], Reply)
            assert len(sent) == 3
            assert len(attempts.records) == 4
            assert attempts.records[-1].error_type == 'GenerationRequestLimitExceeded'
            assert attempts.records[-1].usage is None
            assert model.client.max_retries == 2
        finally:
            await model.client.close()
            await http_client.aclose()

    asyncio.run(run())


def test_bounded_provider_retries_are_agentscope_only_and_raw_sdk_unchanged():
    async def run():
        sent = []

        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(503, json={'error': {'message': 'unavailable', 'type': 'server_error'}},
                                   headers={'retry-after-ms': '0'})

        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=http_client, sdk_retries=2,
                                  generation_budget=GenerationBudget(3, 90))
        model.retry_delay = 0
        try:
            with pytest.raises(InternalServerError):
                await model.generate_structured_output([{'role': 'user', 'content': 'hello'}], Reply)
            assert len(sent) == 3
            assert model.client.max_retries == 2
            sent.clear()
            with pytest.raises(InternalServerError):
                await model.client.chat.completions.create(model='offline-test', messages=[{'role': 'user', 'content': 'raw'}])
            assert len(sent) == 3
        finally:
            await model.client.close()
            await http_client.aclose()

    asyncio.run(run())


def test_validation_fallback_succeeds_with_two_sends_and_legacy_billing():
    async def run():
        sent = []

        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion('invalid-json' if len(sent) == 1 else '{"prose":"ok"}'))

        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=http_client, sdk_retries=2,
                                  generation_budget=GenerationBudget(3, 90))
        try:
            with collect_attempts() as attempts, collect_usage() as billing:
                response = await model.generate_structured_output([{'role': 'user', 'content': 'hello'}], Reply)
            assert response.content == {'prose': 'ok'}
            assert len(sent) == 2
            assert [a.outcome for a in attempts.records] == ['failed', 'succeeded']
            assert len(billing.records) == 1
        finally:
            await model.client.close()
            await http_client.aclose()

    asyncio.run(run())


def test_third_send_is_allowed_and_context_rejection_sends_nothing():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            answer = 'invalid-json' if len(sent) < 3 else '{"prose":"ok"}'
            return httpx2.Response(200, json=completion(answer))
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client,
                                  generation_budget=GenerationBudget(3, 90))
        model.max_retries = 0
        try:
            with pytest.raises(ContextBudgetExceeded):
                await model.generate_structured_output(
                    [{'role': 'user', 'content': 'too large' * 2000}], Reply,
                    context_budget=ContextBudget(context_window_tokens=1500,
                                                 output_tokens=100, safety_margin_tokens=0))
            assert not sent
            response = await model.generate_structured_output(
                [{'role': 'user', 'content': 'hello'}], Reply)
            assert response.content == {'prose': 'ok'}
            assert len(sent) == 3
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_concurrent_structured_and_raw_calls_use_independent_sdk_retry_options():
    async def run():
        sent = []
        entered = asyncio.Event()
        release = asyncio.Event()
        async def transport(request):
            body = json.loads(request.content)
            sent.append(body)
            if 'bounded' in json.dumps(body['messages']):
                entered.set()
                await release.wait()
                return httpx2.Response(200, json=completion())
            return httpx2.Response(503, json={'error': {'message': 'unavailable', 'type': 'server_error'}},
                                   headers={'retry-after-ms': '0'})
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=2,
                                  generation_budget=GenerationBudget(1, 90))
        try:
            bounded = asyncio.create_task(model.generate_structured_output(
                [{'role': 'user', 'content': 'bounded'}], Reply))
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(Exception):
                await model.client.chat.completions.create(
                    model='offline-test', messages=[{'role': 'user', 'content': 'raw'}])
            release.set()
            assert (await bounded).content == {'prose': 'ok'}
            assert len(sent) == 4
            assert client.is_closed is False
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_unbounded_provider_retries_can_produce_nine_transport_sends():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(503, json={'error': {'message': 'unavailable', 'type': 'server_error'}},
                                   headers={'retry-after-ms': '0'})
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=2)
        model.retry_delay = 0
        try:
            with pytest.raises(Exception):
                await model.generate_structured_output([{'role': 'user', 'content': 'hello'}], Reply)
            assert len(sent) == 9
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_explicit_tool_choice_uses_one_bounded_send():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client,
                                  generation_budget=GenerationBudget(1, 90))
        try:
            await model.generate_structured_output(
                [{'role': 'user', 'content': 'hello'}], Reply,
                tool_choice=ToolChoice(mode='generate_structured_output'))
            assert len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_two_models_sharing_transport_keep_separate_allowances():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        first = create_test_model(http_client=client, generation_budget=GenerationBudget(1, 90))
        second = create_test_model(http_client=client, generation_budget=GenerationBudget(2, 90))
        try:
            results = await asyncio.gather(
                first.generate_structured_output([{'role': 'user', 'content': 'first'}], Reply),
                second.generate_structured_output([{'role': 'user', 'content': 'second'}], Reply),
            )
            assert [r.content for r in results] == [{'prose': 'ok'}] * 2
            assert len(sent) == 2 and not client.is_closed
            assert first.client.max_retries == second.client.max_retries == 2
        finally:
            await first.client.close()
            await second.client.close()
            await client.aclose()
    asyncio.run(run())


def test_nested_unbounded_model_does_not_inherit_outer_model_control():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        outer = create_test_model(http_client=client, generation_budget=GenerationBudget(1, 90))
        inner = create_test_model(http_client=client)
        formatter_type = type(outer.formatter)
        original = formatter_type.format
        async def nested(self, messages):
            if self is outer.formatter:
                await inner.generate_structured_output([{'role': 'user', 'content': 'inner'}], Reply)
            return await original(self, messages)
        try:
            with patch.object(formatter_type, 'format', nested):
                await outer.generate_structured_output([{'role': 'user', 'content': 'outer'}], Reply)
            assert len(sent) == 2
        finally:
            await outer.client.close()
            await inner.client.close()
            await client.aclose()
    asyncio.run(run())


def test_nested_ordinary_call_on_same_model_does_not_consume_structured_allowance():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(1, 90))
        formatter_type = type(model.formatter)
        original = formatter_type.format
        nested = False
        async def nested_format(self, messages):
            nonlocal nested
            if not nested:
                nested = True
                await model([{'role': 'user', 'content': 'ordinary'}])
            return await original(self, messages)
        try:
            with patch.object(formatter_type, 'format', nested_format):
                await model.generate_structured_output([{'role': 'user', 'content': 'outer'}], Reply)
            assert len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_child_raw_sdk_task_does_not_inherit_parent_structured_allowance():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(1, 90))
        formatter_type = type(model.formatter)
        original = formatter_type.format
        async def spawn_raw(self, messages):
            await asyncio.create_task(model.client.chat.completions.create(
                model='offline-test', messages=[{'role': 'user', 'content': 'raw'}]))
            return await original(self, messages)
        try:
            with patch.object(formatter_type, 'format', spawn_raw):
                await model.generate_structured_output([{'role': 'user', 'content': 'outer'}], Reply)
            assert len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())
