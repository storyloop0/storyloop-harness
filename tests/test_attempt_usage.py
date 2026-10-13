"""Logical structured attempts observed through the real offline SDK."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from dataclasses import FrozenInstanceError
from agentscope.tool import ToolChoice
from pydantic import BaseModel

from model_helpers import create_test_model
from storyloop_harness.usage import collect_attempts, collect_usage
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded
from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.models.attempt_tracking import AttemptTracker


class Reply(BaseModel):
    prose: str


def completion(arguments, prompt, completion, *, cached=None):
    return {
        "id": "offline", "created": 0, "model": "offline-test", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "tool", "type": "function",
                "function": {"name": "generate_structured_output", "arguments": arguments}}],
        }}],
        "usage": ({"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion,
                  **({"prompt_tokens_details": {"cached_tokens": cached}} if cached is not None else {})}
                  if prompt is not None else None),
    }


def test_failed_validation_preserves_usage_independent_of_billing():
    async def run():
        replies = iter([
            completion('{"unexpected":"invalid"}', 100, 10),
            completion('{"prose":"ok"}', 200, 20),
        ])

        async def transport(_request):
            return httpx2.Response(200, json=next(replies))

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        model = create_test_model(http_client=client, sdk_retries=0)
        model.max_retries = 0
        try:
            with collect_attempts() as attempts, collect_usage() as billing:
                response = await model.generate_structured_output(
                    [{'role': 'user', 'content': 'synthetic'}], Reply,
                )
            assert response.content == {'prose': 'ok'}
            assert [r.outcome for r in attempts.records] == ['failed', 'succeeded']
            assert [r.ordinal for r in attempts.records] == [1, 2]
            assert len({r.invocation_id for r in attempts.records}) == 1
            assert [(r.usage.input_tokens, r.usage.output_tokens) for r in attempts.records] == [(100, 10), (200, 20)]
            assert [r.error_type for r in attempts.records] == ['StructuredOutputError', None]
            assert all(r.stream_complete for r in attempts.records)
            assert [(r.input_tokens, r.output_tokens) for r in billing.records] == [(200, 20)]
        finally:
            await model.client.close()
            await client.aclose()

    asyncio.run(run())


def test_public_record_validation_and_immutable_snapshot():
    from storyloop_harness.usage import ModelAttempt, ModelUsage
    usage = ModelUsage('m', 't', 10, 2)
    record = ModelAttempt('invocation', 1, 'm', 't', 'succeeded', usage,
                          'reported', True, None)
    with pytest.raises(FrozenInstanceError):
        record.ordinal = 2
    with pytest.raises(ValueError):
        ModelAttempt('invocation', 0, 'm', 't', 'succeeded', usage,
                     'reported', True, None)
    with pytest.raises(ValueError):
        ModelAttempt('invocation', 1, 'm', 't', 'succeeded', None,
                     'reported', True, None)
    with pytest.raises(ValueError):
        ModelAttempt('invocation', 1, 'm', 't', 'failed', None,
                     'missing', False, 42)


def test_nested_collectors_route_to_inner_and_restore_outer():
    async def run():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=completion('{"prose":"ok"}', 4, 2))))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            with collect_attempts() as outer:
                await model.generate_structured_output([{'role': 'user', 'content': 'a'}], Reply)
                with collect_attempts() as inner:
                    await model.generate_structured_output([{'role': 'user', 'content': 'b'}], Reply)
                await model.generate_structured_output([{'role': 'user', 'content': 'c'}], Reply)
            assert [r.ordinal for r in outer.records] == [1, 1]
            assert len(inner.records) == 1
            assert len({r.invocation_id for r in outer.records + inner.records}) == 3
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_missing_zero_invalid_and_cached_usage_are_distinct():
    async def run():
        replies = iter([
            completion('{"prose":"ok"}', None, None),
            completion('{"prose":"ok"}', 0, 0),
            completion('{"prose":"ok"}', -1, 2),
            completion('{"prose":"ok"}', 10, 2, cached=3),
        ])
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=next(replies))))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            with collect_attempts() as attempts:
                for _ in range(4):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'synthetic'}], Reply)
            assert [r.usage_state for r in attempts.records] == [
                'missing', 'reported', 'invalid', 'reported']
            assert [r.usage for r in attempts.records[:3]] == [None, attempts.records[1].usage, None]
            assert attempts.records[1].usage.input_tokens == 0
            assert attempts.records[3].usage.cached_input_tokens == 3
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_all_validation_failures_keep_each_reported_usage():
    async def run():
        replies = iter([completion('{"unexpected":1}', n, n // 10) for n in (100, 200, 300)])
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=next(replies))))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            with collect_attempts() as attempts:
                with pytest.raises(Exception) as error:
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'synthetic'}], Reply)
            assert type(error.value).__name__ == 'StructuredOutputError'
            assert [r.usage.input_tokens for r in attempts.records] == [100, 200, 300]
            assert [r.outcome for r in attempts.records] == ['failed'] * 3
            assert all(r.stream_complete for r in attempts.records)
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_explicit_tool_choice_and_budget_rejection_each_record_one_attempt():
    async def run():
        sent = []
        def responder(request):
            sent.append(request)
            return httpx2.Response(200, json=completion('{"unexpected":1}', 4, 2))
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(responder))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            with collect_attempts() as attempts:
                with pytest.raises(Exception):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'x'}], Reply,
                        tool_choice=ToolChoice(mode='auto'))
                with pytest.raises(ContextBudgetExceeded):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'x'}], Reply,
                        context_budget=ContextBudget(context_window_tokens=2,
                                                     output_tokens=1, safety_margin_tokens=0))
            assert len(sent) == 1
            assert len(attempts.records) == 2
            assert attempts.records[1].usage is None
            assert attempts.records[1].error_type == 'ContextBudgetExceeded'
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_sdk_http_retries_do_not_create_logical_attempts():
    async def run():
        sent = []
        def unavailable(request):
            sent.append(request)
            return httpx2.Response(503, headers={'retry-after-ms': '1'},
                                   json={'error': {'message': 'offline unavailable', 'type': 'server_error'}})
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(unavailable))
        model = create_test_model(http_client=client, sdk_retries=2)
        model.retry_delay = 0
        try:
            with collect_attempts() as attempts:
                with pytest.raises(Exception):
                    await model.generate_structured_output(
                        [{'role': 'user', 'content': 'synthetic'}], Reply)
            assert len(sent) == 9
            assert len(attempts.records) == 3
            assert [r.ordinal for r in attempts.records] == [1, 2, 3]
            assert all(r.usage is None and r.usage_state == 'missing' for r in attempts.records)
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_sdk_recovers_within_one_logical_attempt():
    async def run():
        sent = []
        def responder(request):
            sent.append(request)
            if len(sent) == 1:
                return httpx2.Response(503, headers={'retry-after-ms': '1'},
                                       json={'error': {'message': 'offline unavailable', 'type': 'server_error'}})
            return httpx2.Response(200, json=completion('{"prose":"ok"}', 8, 3))
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(responder))
        model = create_test_model(http_client=client, sdk_retries=2)
        model.retry_delay = 0
        try:
            with collect_attempts() as attempts:
                await model.generate_structured_output(
                    [{'role': 'user', 'content': 'synthetic'}], Reply)
            assert len(sent) == 2
            assert len(attempts.records) == 1
            assert attempts.records[0].usage.input_tokens == 8
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_concurrent_invocations_on_shared_model_keep_local_ordinals():
    async def run():
        arrived = asyncio.Event()
        release = asyncio.Event()
        count = 0
        async def responder(_request):
            nonlocal count
            count += 1
            if count == 2:
                arrived.set()
            await release.wait()
            return httpx2.Response(200, json=completion('{"prose":"ok"}', count, 1))
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(responder))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            with collect_attempts() as attempts:
                one = asyncio.create_task(model.generate_structured_output(
                    [{'role': 'user', 'content': 'one'}], Reply))
                two = asyncio.create_task(model.generate_structured_output(
                    [{'role': 'user', 'content': 'two'}], Reply))
                await asyncio.wait_for(arrived.wait(), 5)
                release.set()
                await asyncio.gather(one, two)
            assert len(attempts.records) == 2
            assert [r.ordinal for r in attempts.records] == [1, 1]
            assert len({r.invocation_id for r in attempts.records}) == 2
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_engine_failure_keeps_attempts_in_caller_scope(tmp_path):
    async def run():
        package = ScenarioPackage.load(Path(__file__).parents[1] / 'examples/freeform')
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=completion('{"unexpected":1}', 9, 4))))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            with SQLiteGameStore(tmp_path / 'attempts.db') as store:
                package.seed_game(store, 'game')
                engine = TurnEngine(store, package, model)
                with collect_attempts() as attempts:
                    with pytest.raises(Exception):
                        await engine.run_turn(TurnInput('game', 'Hello', 'turn-1', package.version))
                assert attempts.records
                assert all(r.usage.input_tokens == 9 for r in attempts.records)
                assert all(r.outcome == 'failed' for r in attempts.records)
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_snapshot_is_detached_from_mutable_source_usage():
    async def run():
        tracker = AttemptTracker('model-one', 'task-one')
        source = SimpleNamespace(input_tokens=12, output_tokens=3,
                                 cache_input_tokens=2, metadata={'secret': 'do not retain'})
        with collect_attempts() as attempts, tracker.invocation():
            async with tracker.attempt():
                tracker.observe(SimpleNamespace(usage=source))
                source.input_tokens = 999
                source.metadata['secret'] = 'changed'
        record = attempts.records[0]
        assert record.usage.input_tokens == 12
        assert record.usage.cached_input_tokens == 2
        assert 'secret' not in repr(record)
        assert not hasattr(record, 'metadata')
    asyncio.run(run())


def test_no_collector_does_not_leak_into_later_collector():
    async def run():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=completion('{"prose":"ok"}', 5, 1))))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            await model.generate_structured_output([{'role': 'user', 'content': 'outside'}], Reply)
            with collect_attempts() as first:
                await model.generate_structured_output([{'role': 'user', 'content': 'first'}], Reply)
            await model.generate_structured_output([{'role': 'user', 'content': 'outside'}], Reply)
            with collect_attempts() as second:
                await model.generate_structured_output([{'role': 'user', 'content': 'second'}], Reply)
            assert len(first.records) == len(second.records) == 1
            assert first.records[0].invocation_id != second.records[0].invocation_id
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())


def test_two_models_keep_their_own_model_and_task_labels():
    async def run():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=completion('{"prose":"ok"}', 5, 1))))
        first = create_test_model(model='first', task='draft', http_client=client, sdk_retries=0)
        second = create_test_model(model='second', task='review', http_client=client, sdk_retries=0)
        try:
            with collect_attempts() as attempts:
                await asyncio.gather(
                    first.generate_structured_output([{'role': 'user', 'content': 'one'}], Reply),
                    second.generate_structured_output([{'role': 'user', 'content': 'two'}], Reply),
                )
            assert {(r.model, r.task) for r in attempts.records} == {
                ('first', 'draft'), ('second', 'review')}
            assert all(r.ordinal == 1 for r in attempts.records)
        finally:
            await first.client.close()
            await second.client.close()
            await client.aclose()
    asyncio.run(run())


def test_nested_failure_restores_outer_collector():
    async def run():
        replies = iter([
            completion('{"prose":"ok"}', 1, 1),
            completion('{"unexpected":1}', 2, 1),
            completion('{"prose":"ok"}', 3, 1),
        ])
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=next(replies))))
        model = create_test_model(http_client=client, sdk_retries=0)
        try:
            with collect_attempts() as outer:
                await model.generate_structured_output([{'role': 'user', 'content': 'before'}], Reply)
                with collect_attempts() as inner:
                    with pytest.raises(Exception):
                        await model.generate_structured_output(
                            [{'role': 'user', 'content': 'fail'}], Reply,
                            tool_choice=ToolChoice(mode='auto'))
                await model.generate_structured_output([{'role': 'user', 'content': 'after'}], Reply)
            assert [r.usage.input_tokens for r in outer.records] == [1, 3]
            assert [r.usage.input_tokens for r in inner.records] == [2]
            assert inner.records[0].outcome == 'failed'
        finally:
            await model.client.close()
            await client.aclose()
    asyncio.run(run())
