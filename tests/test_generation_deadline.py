"""Elapsed generation deadlines across formatting, provider calls, and validation."""
import asyncio
import json
import time
from unittest.mock import patch

import httpx2
import pytest
from pydantic import BaseModel

from model_helpers import create_test_model
from storyloop_harness.generation import GenerationBudget, GenerationTimeout
from storyloop_harness.usage import collect_attempts


class Reply(BaseModel):
    prose: str


def completion():
    return {
        'id': 'offline', 'created': 0, 'model': 'offline-test', 'object': 'chat.completion',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'tool', 'type': 'function',
                'function': {'name': 'generate_structured_output', 'arguments': '{"prose":"ok"}'}}],
        }}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5},
    }


def test_timeout_during_formatter_prevents_send_and_scope_resets():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(3, 0.02))
        formatter_type = type(model.formatter)
        original = formatter_type.format
        async def slow(self, messages):
            await asyncio.sleep(0.1)
            return await original(self, messages)
        try:
            with patch.object(formatter_type, 'format', slow):
                with pytest.raises(GenerationTimeout, match='^Structured generation timeout exceeded$'):
                    await asyncio.wait_for(model.generate_structured_output(
                        [{'role': 'user', 'content': 'slow'}], Reply), 1)
            assert sent == []
            await model.client.chat.completions.create(
                model='offline-test', messages=[{'role': 'user', 'content': 'raw'}])
            assert len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_timeout_during_provider_records_failed_attempt_and_no_later_send():
    async def run():
        sent = []
        entered = asyncio.Event()
        async def transport(request):
            sent.append(json.loads(request.content))
            entered.set()
            await asyncio.sleep(1)
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(3, 0.2))
        try:
            with collect_attempts() as attempts:
                with pytest.raises(GenerationTimeout):
                    await asyncio.wait_for(model.generate_structured_output(
                        [{'role': 'user', 'content': 'slow'}], Reply), 1)
            assert entered.is_set() and len(sent) == 1
            assert attempts.records[-1].error_type == 'GenerationTimeout'
            assert attempts.records[-1].usage is None
            await asyncio.sleep(0.03)
            assert len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_user_cancellation_stays_cancelled_error():
    async def run():
        entered = asyncio.Event()
        async def transport(_request):
            entered.set()
            await asyncio.sleep(1)
            return httpx2.Response(200, json=completion())
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(3, 90))
        try:
            with collect_attempts() as attempts:
                task = asyncio.create_task(model.generate_structured_output(
                    [{'role': 'user', 'content': 'cancel'}], Reply))
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert attempts.records[-1].outcome == 'cancelled'
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_late_synchronous_schema_validation_rejected():
    async def run():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=completion())))
        model = create_test_model(http_client=client, generation_budget=GenerationBudget(3, 0.02))
        original = Reply.model_validate
        def slow_validate(value, *args, **kwargs):
            time.sleep(0.04)
            return original(value, *args, **kwargs)
        try:
            with patch.object(Reply, 'model_validate', slow_validate):
                with pytest.raises(GenerationTimeout):
                    await asyncio.wait_for(model.generate_structured_output(
                        [{'role': 'user', 'content': 'late'}], Reply), 1)
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_provider_timeout_error_is_not_relabelled_as_generation_deadline():
    async def run():
        async def transport(_request):
            raise TimeoutError('provider timeout')
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0,
                                  generation_budget=GenerationBudget(3, 90))
        model.max_retries = 0
        try:
            with pytest.raises(TimeoutError, match='provider timeout') as error:
                await model.generate_structured_output([{'role': 'user', 'content': 'hi'}], Reply)
            assert type(error.value) is TimeoutError
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_deadline_during_sse_keeps_reported_usage_and_closes_source():
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
            lambda _: httpx2.Response(200, headers={'content-type': 'text/event-stream'},
                                      stream=stream)))
        model = create_test_model(http_client=client, sdk_retries=0,
                                  generation_budget=GenerationBudget(3, 0.2))
        model.stream = True
        model.max_retries = 0
        try:
            with collect_attempts() as attempts:
                with pytest.raises(GenerationTimeout):
                    await asyncio.wait_for(model.generate_structured_output(
                        [{'role': 'user', 'content': 'stream'}], Reply), 1)
            assert stream.waiting.is_set() and stream.closed
            assert attempts.records[-1].error_type == 'GenerationTimeout'
            assert attempts.records[-1].usage.input_tokens == 10
            assert attempts.records[-1].stream_complete is False
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_deadline_covers_agentscope_retry_wait():
    async def run():
        sent = []
        async def transport(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(503, json={'error': {'message': 'unavailable', 'type': 'server_error'}},
                                   headers={'retry-after-ms': '0'})
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=2,
                                  generation_budget=GenerationBudget(3, 0.2))
        model.retry_delay = 1
        try:
            with pytest.raises(GenerationTimeout):
                await asyncio.wait_for(model.generate_structured_output(
                    [{'role': 'user', 'content': 'retry'}], Reply), 2)
            assert len(sent) == 1
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())
