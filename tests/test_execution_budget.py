"""Turn-wide request admission through the actual SDK transport."""
import asyncio
import json
from dataclasses import FrozenInstanceError

import httpx2
import pytest
from openai import InternalServerError
from pydantic import BaseModel

from model_helpers import create_test_model
from storyloop_harness.generation import (
    ExecutionBudget, ExecutionRequestLimitExceeded, GenerationBudget,
    GenerationRequestLimitExceeded, execution_budget_scope,
)
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded


class Reply(BaseModel):
    prose: str


def completion():
    return {'id': 'offline', 'created': 0, 'model': 'offline-test',
            'object': 'chat.completion', 'choices': [{'index': 0,
            'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
            'content': None, 'tool_calls': [{'id': 'tool', 'type': 'function',
            'function': {'name': 'generate_structured_output',
            'arguments': '{"prose":"ok"}'}}]}}]}


def test_public_policy_validation_and_immutability():
    assert ExecutionBudget() == ExecutionBudget(6, 180)
    for value in (0, -1, True, 1.5, '6'):
        with pytest.raises((TypeError, ValueError)):
            ExecutionBudget(max_requests=value)
    for value in (0, -1, True, '180', float('nan'), float('inf')):
        with pytest.raises((TypeError, ValueError)):
            ExecutionBudget(timeout_seconds=value)
    with pytest.raises(FrozenInstanceError):
        ExecutionBudget().max_requests = 1


def test_nested_scope_is_shared_and_child_cannot_reopen():
    async def run():
        async with execution_budget_scope(ExecutionBudget(2, 30)) as outer:
            async with execution_budget_scope(ExecutionBudget(2, 30)) as inner:
                assert inner is outer and inner.deadline == outer.deadline
            with pytest.raises(ValueError):
                async with execution_budget_scope(ExecutionBudget(3, 30)):
                    pass
            async def child():
                async with execution_budget_scope(ExecutionBudget(2, 30)):
                    pass
            with pytest.raises(ExecutionRequestLimitExceeded):
                await asyncio.create_task(child())
        async with execution_budget_scope(ExecutionBudget(2, 30)) as fresh:
            assert fresh is not outer and fresh.sends == 0
    asyncio.run(run())


def test_two_models_share_six_sends_and_seventh_is_blocked():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        first = create_test_model(http_client=client, task='first')
        second = create_test_model(http_client=client, task='second')
        first.max_retries = second.max_retries = 0
        try:
            async with execution_budget_scope(ExecutionBudget(6, 30)) as control:
                for index in range(6):
                    model = first if index % 2 else second
                    await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply)
                with pytest.raises(ExecutionRequestLimitExceeded):
                    await first.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply)
                assert control.sends == len(sent) == 6
                assert all('execution_budget' not in body and 'generation_budget' not in body for body in sent)
        finally:
            await first.client.close()
            await second.client.close()
            await client.aclose()
    asyncio.run(run())


def test_execution_only_disables_sdk_retries_and_claims_failed_send():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(503, json={'error': {'message': 'unavailable', 'type': 'server_error'}},
                                   headers={'retry-after-ms': '0'})
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=2)
        model.max_retries = 0
        try:
            async with execution_budget_scope(ExecutionBudget(1, 30)) as control:
                with pytest.raises(InternalServerError):
                    await model.client.chat.completions.create(model='offline-test', messages=[{'role': 'user', 'content': 'hi'}])
                assert control.sends == len(sent) == 1
                with pytest.raises(ExecutionRequestLimitExceeded):
                    await model.client.chat.completions.create(model='offline-test', messages=[{'role': 'user', 'content': 'hi'}])
            assert len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_local_generation_rejection_does_not_claim_execution_send():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            payload = completion()
            if len(sent) == 1:
                payload['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = 'invalid-json'
            return httpx2.Response(200, json=payload)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client,
                                  generation_budget=GenerationBudget(1, 30))
        model.max_retries = 0
        try:
            async with execution_budget_scope(ExecutionBudget(2, 30)) as control:
                with pytest.raises(GenerationRequestLimitExceeded):
                    await model.generate_structured_output([{'role': 'user', 'content': 'invalid'}], Reply)
                assert control.sends == len(sent) == 1
                await model.generate_structured_output([{'role': 'user', 'content': 'valid'}], Reply)
                assert control.sends == len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_context_rejection_does_not_claim_execution_send():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client)
        try:
            async with execution_budget_scope(ExecutionBudget(1, 30)) as control:
                with pytest.raises(ContextBudgetExceeded):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'too large' * 2000}], Reply,
                        context_budget=ContextBudget(context_window_tokens=1500,
                                                     output_tokens=100, safety_margin_tokens=0))
                assert control.sends == 0 and sent == []
                await model.generate_structured_output([{'role': 'user', 'content': 'ok'}], Reply)
                assert control.sends == len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_separate_top_level_tasks_have_independent_scopes():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            await asyncio.sleep(0)
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client)
        async def one_turn():
            async with execution_budget_scope(ExecutionBudget(1, 30)) as control:
                await model.client.chat.completions.create(
                    model='offline-test', messages=[{'role': 'user', 'content': 'hi'}])
                assert control.sends == 1
        try:
            await asyncio.gather(one_turn(), one_turn())
            assert len(sent) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_invalid_sdk_body_does_not_claim_execution_send():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client)
        try:
            async with execution_budget_scope(ExecutionBudget(1, 30)) as control:
                with pytest.raises(ValueError, match='budget-critical'):
                    await model.client.chat.completions.create(
                        model='offline-test', messages=[{'role': 'user', 'content': 'hi'}],
                        extra_body={'messages': []})
                assert control.sends == 0 and sent == []
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())
