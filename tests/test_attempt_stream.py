"""Real SDK SSE paths retain last reported usage and release streams."""
import asyncio
import json

import httpx2
import pytest
from pydantic import BaseModel
from types import SimpleNamespace

from model_helpers import create_test_model
from storyloop_harness.usage import collect_attempts
from storyloop_harness.models.attempt_tracking import AttemptTracker


class Reply(BaseModel):
    prose: str


def event(*, arguments=None, prompt=None, completion=None):
    choices = []
    if arguments is not None:
        choices = [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "tool",
                    "type": "function", "function": {
                        "name": "generate_structured_output", "arguments": arguments}}]},
                    "finish_reason": None}]
    payload = {"id": "offline", "created": 0, "model": "test-model",
               "object": "chat.completion.chunk", "choices": choices,
               "usage": ({"prompt_tokens": prompt, "completion_tokens": completion,
                          "total_tokens": prompt + completion}
                         if prompt is not None else None)}
    return f"data: {json.dumps(payload)}\n\n".encode()


class ControlledStream(httpx2.AsyncByteStream):
    def __init__(self, pieces, *, error=None, gate=None):
        self.pieces = pieces
        self.error = error
        self.gate = gate
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for piece in self.pieces:
            yield piece
        if self.gate is not None:
            self.waiting.set()
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


async def run_stream(stream, *, bad=False):
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=stream)))
    model = create_test_model(http_client=client, sdk_retries=0)
    model.stream = True
    model.max_retries = 0
    try:
        with collect_attempts() as attempts:
            if bad:
                with pytest.raises(Exception):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'synthetic'}], Reply)
            else:
                response = await model.generate_structured_output(
                    [{'role': 'user', 'content': 'synthetic'}], Reply)
                assert response.content == {'prose': 'ok'}
        return attempts.records
    finally:
        await model.client.close()
        await client.aclose()


def test_completed_stream_retains_final_cumulative_usage_even_after_validation_failure():
    async def run():
        for arguments, outcome in [('{"prose":"ok"}', 'succeeded'),
                                   ('{"unexpected":1}', 'failed')]:
            stream = ControlledStream([
                event(arguments=arguments), event(prompt=10, completion=1),
                event(prompt=20, completion=2),
            ])
            records = await run_stream(stream, bad=outcome == 'failed')
            assert len(records) == (1 if outcome == 'succeeded' else 3)
            assert all(r.outcome == outcome for r in records)
            assert all(r.usage.input_tokens == 20 for r in records)
            assert all(r.usage.output_tokens == 2 for r in records)
            assert all(r.stream_complete for r in records)
            assert stream.closed
    asyncio.run(run())


def test_stream_read_error_preserves_last_usage_and_closes_resource():
    async def run():
        stream = ControlledStream([event(arguments='{"prose":"ok"}'),
                                   event(prompt=10, completion=1)],
                                  error=httpx2.ReadError('offline read failure'))
        records = await run_stream(stream, bad=True)
        assert len(records) == 1
        assert records[0].outcome == 'failed'
        # OpenAI's SDK wraps a transport ReadError as APIConnectionError.
        assert records[0].error_type == 'APIConnectionError'
        assert records[0].usage.input_tokens == 10
        assert not records[0].stream_complete
        assert stream.closed
    asyncio.run(run())


def test_cancellation_before_and_after_usage_closes_resource():
    async def run():
        for pieces, expected in [([], None), ([event(prompt=10, completion=1)], 10)]:
            gate = asyncio.Event()
            stream = ControlledStream(pieces, gate=gate)
            client = httpx2.AsyncClient(transport=httpx2.MockTransport(
                lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                          stream=stream)))
            model = create_test_model(http_client=client, sdk_retries=0)
            model.stream = True
            model.max_retries = 0
            try:
                with collect_attempts() as attempts:
                    task = asyncio.create_task(model.generate_structured_output(
                        [{'role': 'user', 'content': 'synthetic'}], Reply))
                    await asyncio.wait_for(stream.waiting.wait(), 5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert len(attempts.records) == 1
                assert attempts.records[0].outcome == 'cancelled'
                assert attempts.records[0].stream_complete is False
                assert (attempts.records[0].usage.input_tokens
                        if attempts.records[0].usage is not None else None) == expected
                assert stream.closed
            finally:
                await model.client.close()
                await client.aclose()
    asyncio.run(run())


def test_observer_cleanup_does_not_replace_primary_stream_error():
    """Boundary regression: an early stream error and a failing close."""
    class FaultingIterator:
        def __init__(self):
            self.reads = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.reads += 1
            if self.reads == 1:
                return SimpleNamespace(usage=SimpleNamespace(
                    input_tokens=5, output_tokens=1, cache_input_tokens=0))
            raise httpx2.ReadError('read failed')

        async def aclose(self):
            self.closed = True
            raise RuntimeError('close failed')

    async def run():
        tracker = AttemptTracker('m', 'task')
        source = FaultingIterator()
        with collect_attempts() as attempts, tracker.invocation():
            with pytest.raises(httpx2.ReadError):
                async with tracker.attempt():
                    async for _ in tracker.observe(source):
                        pass
        assert source.closed
        assert attempts.records[0].error_type == 'ReadError'
        assert attempts.records[0].usage.input_tokens == 5
        assert not attempts.records[0].stream_complete
    asyncio.run(run())


def test_attempt_exit_closes_early_stream_and_keeps_incomplete_usage():
    """Boundary regression: a parent that stops before normal exhaustion."""
    async def run():
        closed = False
        async def source():
            nonlocal closed
            try:
                yield SimpleNamespace(usage=SimpleNamespace(
                    input_tokens=7, output_tokens=2, cache_input_tokens=0))
                await asyncio.Event().wait()
            finally:
                closed = True
        tracker = AttemptTracker('m', 'task')
        with collect_attempts() as attempts, tracker.invocation():
            async with tracker.attempt():
                observed = tracker.observe(source())
                await anext(observed)
        assert closed
        assert attempts.records[0].usage.input_tokens == 7
        assert attempts.records[0].stream_complete is False
    asyncio.run(run())


def test_cleanup_error_does_not_replace_primary_cancellation():
    class ClosingError:
        def __aiter__(self):
            return self
        async def __anext__(self):
            return SimpleNamespace(usage=None)
        async def aclose(self):
            raise RuntimeError('close failed')

    async def run():
        tracker = AttemptTracker('m', 'task')
        with collect_attempts() as attempts, tracker.invocation():
            with pytest.raises(asyncio.CancelledError):
                async with tracker.attempt():
                    observed = tracker.observe(ClosingError())
                    await anext(observed)
                    raise asyncio.CancelledError()
        assert attempts.records[0].outcome == 'cancelled'
        assert attempts.records[0].error_type == 'CancelledError'
    asyncio.run(run())


def test_later_invalid_usage_supersedes_prior_valid_snapshot():
    async def source():
        yield SimpleNamespace(usage=SimpleNamespace(
            input_tokens=5, output_tokens=1, cache_input_tokens=0))
        yield SimpleNamespace(usage=None)
        yield SimpleNamespace(usage=SimpleNamespace(
            input_tokens=-1, output_tokens=2, cache_input_tokens=0))

    async def run():
        tracker = AttemptTracker('m', 'task')
        with collect_attempts() as attempts, tracker.invocation():
            async with tracker.attempt():
                async for _ in tracker.observe(source()):
                    pass
        assert attempts.records[0].usage is None
        assert attempts.records[0].usage_state == 'invalid'
        assert attempts.records[0].stream_complete
    asyncio.run(run())


def test_cleanup_error_after_normal_stream_is_recorded_as_failure():
    class CloseFailure:
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration
        async def aclose(self):
            raise RuntimeError('close failed')

    async def run():
        tracker = AttemptTracker('m', 'task')
        with collect_attempts() as attempts, tracker.invocation():
            with pytest.raises(RuntimeError, match='close failed'):
                async with tracker.attempt():
                    async for _ in tracker.observe(CloseFailure()):
                        pass
        assert attempts.records[0].outcome == 'failed'
        assert attempts.records[0].error_type == 'RuntimeError'
    asyncio.run(run())
