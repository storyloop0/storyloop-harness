"""Elapsed execution deadlines and cancellation provenance."""
import asyncio
import json
import time
from unittest.mock import patch

import httpx2
import pytest
from pydantic import BaseModel

from model_helpers import create_test_model
from storyloop_harness.generation import (
    ExecutionBudget, ExecutionRequestLimitExceeded, ExecutionTimeout,
    GenerationBudget, GenerationTimeout, execution_budget_scope,
)
from storyloop_harness.runtime.turn_progress import emit
from storyloop_harness.models.execution_control import current_execution_control
from storyloop_harness.usage import collect_attempts


class Reply(BaseModel):
    prose: str


def completion():
    return {'id': 'offline', 'created': 0, 'model': 'offline-test',
            'object': 'chat.completion', 'choices': [{'index': 0,
            'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
            'content': None, 'tool_calls': [{'id': 'tool', 'type': 'function',
            'function': {'name': 'generate_structured_output',
            'arguments': '{"prose":"ok"}'}}]}}]}


def test_execution_deadline_covers_two_generations_and_scope_resets():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            await asyncio.sleep(0.06)
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(3, 1))
        try:
            with pytest.raises(ExecutionTimeout):
                async with execution_budget_scope(ExecutionBudget(6, 0.1)):
                    await model.generate_structured_output([{'role': 'user', 'content': 'a'}], Reply)
                    await asyncio.sleep(0.06)
                    await model.generate_structured_output([{'role': 'user', 'content': 'b'}], Reply)
            assert len(sent) == 1
            async with execution_budget_scope(ExecutionBudget(6, 1)) as fresh:
                assert fresh.sends == 0
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('execution,local,expected', [
    (0.05, 0.2, ExecutionTimeout), (0.2, 0.05, GenerationTimeout),
])
def test_earlier_deadline_wins_during_provider_wait(execution, local, expected):
    async def run():
        entered = asyncio.Event()
        async def transport(_request):
            entered.set()
            await asyncio.Event().wait()
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0,
                                  generation_budget=GenerationBudget(3, local))
        try:
            with pytest.raises(expected):
                async with execution_budget_scope(ExecutionBudget(6, execution)):
                    await model.generate_structured_output([{'role': 'user', 'content': 'a'}], Reply)
            assert entered.is_set()
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_external_cancel_keeps_identity():
    async def run():
        entered = asyncio.Event()
        async def transport(_request):
            entered.set()
            await asyncio.Event().wait()
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0,
                                  generation_budget=GenerationBudget(3, 1))
        async def call():
            async with execution_budget_scope(ExecutionBudget(6, 0.2)):
                await model.generate_structured_output([{'role': 'user', 'content': 'a'}], Reply)
        try:
            task = asyncio.create_task(call())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_late_synchronous_validation_and_ordinary_call_are_checked():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(3, 1))
        original = Reply.model_validate
        def slow(value, *args, **kwargs):
            current_execution_control().deadline = time.monotonic() - 1
            return original(value, *args, **kwargs)
        try:
            with pytest.raises(ExecutionTimeout):
                async with execution_budget_scope(ExecutionBudget(6, 1)):
                    with patch.object(Reply, 'model_validate', slow):
                        await model.generate_structured_output([{'role': 'user', 'content': 'a'}], Reply)
            assert len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_child_raw_sdk_send_rejected_before_transport():
    async def run():
        sent = []
        async def transport(request):
            sent.append(request)
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client)
        try:
            async with execution_budget_scope(ExecutionBudget(6, 1)):
                with pytest.raises(ExecutionRequestLimitExceeded):
                    await asyncio.create_task(model.client.chat.completions.create(
                        model='offline-test', messages=[{'role': 'user', 'content': 'hi'}]))
            assert sent == []
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_execution_deadline_covers_lazy_sse_consumption_and_preserves_usage():
    class Stream(httpx2.AsyncByteStream):
        def __init__(self):
            self.closed = False
            self.waiting = asyncio.Event()

        async def __aiter__(self):
            payload = {'id': 'offline', 'created': 0, 'model': 'test-model',
                       'object': 'chat.completion.chunk', 'choices': [],
                       'usage': {'prompt_tokens': 10, 'completion_tokens': 1,
                                 'total_tokens': 11}}
            yield f'data: {json.dumps(payload)}\n\n'.encode()
            self.waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    async def run():
        stream = Stream()
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)))
        model = create_test_model(http_client=client, sdk_retries=0)
        model.stream = True
        model.max_retries = 0
        try:
            with collect_attempts() as attempts:
                with pytest.raises(ExecutionTimeout):
                    async with execution_budget_scope(ExecutionBudget(6, 0.1)):
                        await model.generate_structured_output([{'role': 'user', 'content': 'stream'}], Reply)
            assert stream.waiting.is_set() and stream.closed
            assert attempts.records[-1].usage.input_tokens == 10
            assert attempts.records[-1].stream_complete is False
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_ordinary_model_call_in_scope_claims_send():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json={
                'id': 'offline', 'created': 0, 'model': 'offline-test',
                'object': 'chat.completion', 'choices': [{'index': 0,
                'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
            })
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client)
        try:
            async with execution_budget_scope(ExecutionBudget(1, 1)) as control:
                await model([{'role': 'user', 'content': 'hi'}])
                assert control.sends == len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('error', [ExecutionRequestLimitExceeded, ExecutionTimeout])
def test_progress_rethrows_execution_terminal_error(error):
    async def run():
        async def broken(_event):
            raise error()
        with pytest.raises(error):
            await emit(broken, 'test')
    asyncio.run(run())
