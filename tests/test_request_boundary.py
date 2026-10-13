"""The budget guard runs on the one request sent by the real SDK."""
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

import httpx2
import pytest
from pydantic import BaseModel

from model_helpers import create_test_model
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded
from storyloop_harness.context.budget import estimate_request_envelope


class Reply(BaseModel):
    prose: str


class Nested(BaseModel):
    value: str


class Expanded(BaseModel):
    first: Nested
    second: Nested


def completion_payload(arguments='{"prose":"ok"}'):
    return {
        "id": "offline", "created": 0, "model": "offline-test", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "tool", "type": "function",
                "function": {"name": "generate_structured_output", "arguments": arguments}}],
        }}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@asynccontextmanager
async def offline_model(responder=None, **kwargs):
    sent = []

    async def transport(request):
        body = json.loads(request.content)
        sent.append(body)
        if responder is not None:
            return await responder(request, body)
        return httpx2.Response(200, json=completion_payload())

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
    model = create_test_model('offline-test', http_client=http_client, sdk_retries=0, **kwargs)
    try:
        yield model, sent
    finally:
        await model.client.close()
        await http_client.aclose()


def wire_estimate(body, budget):
    return estimate_request_envelope(
        body['messages'], body.get('tools'), budget,
        envelope_options={k: v for k, v in body.items() if k not in ('messages', 'tools')},
    )


def test_bounded_call_formats_only_once_and_sends_formatted_message():
    async def run():
        async with offline_model() as (model, sent):
            formatter_calls = []
            formatter_type = type(model.formatter)
            original_format = formatter_type.format

            async def changing_format(self, messages):
                formatted = await original_format(self, messages)
                formatter_calls.append(len(formatter_calls) + 1)
                marker = f"synthetic-format-{len(formatter_calls)}"
                if isinstance(formatted[-1]["content"], list):
                    formatted[-1]["content"].append({"type": "text", "text": marker})
                else:
                    formatted[-1]["content"] += marker
                return formatted

            with patch.object(formatter_type, "format", changing_format):
                response = await model.generate_structured_output(
                    [{"role": "user", "content": "hello"}], Reply,
                    context_budget=ContextBudget(),
                )
            assert response.content == {"prose": "ok"}
            assert formatter_calls == [1]
            assert len(sent) == 1
            assert "synthetic-format-1" in json.dumps(sent[0]["messages"])

    asyncio.run(run())


def test_exact_wire_boundary_and_output_cap():
    async def run():
        async with offline_model() as (model, sent):
            messages = [{'role': 'user', 'content': 'boundary marker'}]
            budget = ContextBudget(output_tokens=101)
            await model(messages, structured_model=Reply, context_budget=budget)
            assert sent[0]['max_completion_tokens'] == 101
            assert 'context_budget' not in sent[0] and 'max_tokens' not in sent[0]
            report = wire_estimate(sent[0], budget)
            exact = replace(budget, context_window_tokens=report.total_tokens)
            await model(messages, structured_model=Reply, context_budget=exact)
            assert sent[1] == sent[0]
            with pytest.raises(ContextBudgetExceeded) as error:
                await model(messages, structured_model=Reply,
                            context_budget=replace(exact, context_window_tokens=report.total_tokens - 1))
            assert error.value.report.total_tokens == report.total_tokens
            assert len(sent) == 2
    asyncio.run(run())


def test_rejection_happens_before_transport_and_budget_resets():
    async def run():
        async with offline_model() as (model, sent):
            with pytest.raises(ContextBudgetExceeded) as error:
                await model([{'role': 'user', 'content': 'secret-marker' * 2000}],
                            structured_model=Reply,
                            context_budget=ContextBudget(context_window_tokens=1500,
                                                         output_tokens=100, safety_margin_tokens=0))
            assert not sent
            assert 'secret-marker' not in str(error.value)
            assert 'secret-marker' not in json.dumps(error.value.report.to_dict())
            await model([{'role': 'user', 'content': 'ok'}], structured_model=Reply)
            assert len(sent) == 1 and 'max_completion_tokens' not in sent[0]
    asyncio.run(run())


@pytest.mark.parametrize('field', ['messages', 'tools', 'tool_choice', 'max_tokens', 'max_completion_tokens'])
@pytest.mark.parametrize('source', ['model', 'call'])
def test_budget_critical_extra_body_rejected_before_transport(field, source):
    async def run():
        async with offline_model() as (model, sent):
            kwargs = {}
            if source == 'model':
                model.extra_body = {field: 'override'}
            else:
                kwargs['extra_body'] = {field: 'override'}
            with pytest.raises(ValueError, match='budget-critical'):
                await model([{'role': 'user', 'content': 'ok'}], structured_model=Reply,
                            context_budget=ContextBudget(), **kwargs)
            assert not sent
    asyncio.run(run())


@pytest.mark.parametrize('kwargs', [{'max_tokens': 100}, {'max_completion_tokens': 100},
                                    {'max_completion_tokens': True}])
def test_conflicting_output_cap_rejected_before_transport(kwargs):
    async def run():
        async with offline_model() as (model, sent):
            with pytest.raises(ValueError):
                await model([{'role': 'user', 'content': 'ok'}], structured_model=Reply,
                            context_budget=ContextBudget(), **kwargs)
            assert not sent
    asyncio.run(run())


def test_stream_option_and_provider_override_follow_effective_json():
    async def run():
        async def sse(_request, _body):
            return httpx2.Response(200, headers={'content-type': 'text/event-stream'},
                                   content=b'data: [DONE]\n\n')

        async with offline_model(sse) as (model, sent):
            model.stream = True
            model._parse_stream_response = lambda start, response: 'offline stream'
            messages = model._messages([{'role': 'user', 'content': 'stream marker'}])
            extra_body = {'stream_options': {'include_usage': False, 'padding': 'x' * 2000}}
            await model._call_api(model.model, messages, context_budget=ContextBudget())
            assert sent[0]['stream_options'] == {'include_usage': True}
            await model._call_api(model.model, messages, context_budget=ContextBudget(),
                                  extra_body=extra_body)
            assert sent[1]['stream_options'] == extra_body['stream_options']
            report = wire_estimate(sent[1], ContextBudget())
            exact = replace(ContextBudget(), context_window_tokens=report.total_tokens)
            await model._call_api(model.model, messages, context_budget=exact, extra_body=extra_body)
            with pytest.raises(ContextBudgetExceeded):
                await model._call_api(model.model, messages,
                                      context_budget=replace(exact, context_window_tokens=report.total_tokens - 1),
                                      extra_body=extra_body)
            assert len(sent) == 3
    asyncio.run(run())


def test_transport_options_preserved_but_excluded_from_estimate():
    async def run():
        inspected = []
        wire_transport = []

        async def responder(request, _body):
            wire_transport.append(request)
            return httpx2.Response(200, json=completion_payload())

        async with offline_model(responder) as (model, sent):
            from storyloop_harness.models import request_guard
            original = request_guard.estimate_request_envelope

            def capture(messages, tools, budget, *, envelope_options):
                inspected.append(envelope_options)
                return original(messages, tools, budget, envelope_options=envelope_options)

            with patch.object(request_guard, 'estimate_request_envelope', capture):
                await model([{'role': 'user', 'content': 'ok'}], structured_model=Reply,
                            context_budget=ContextBudget(), timeout=httpx2.Timeout(3, connect=1),
                            extra_headers={'X-Offline': 'yes'}, extra_query={'offline': 'yes'})
            assert len(sent) == 1
            assert not {'timeout', 'extra_headers', 'extra_query'}.intersection(inspected[0])
            assert wire_transport[0].headers['X-Offline'] == 'yes'
            assert wire_transport[0].url.params['offline'] == 'yes'
    asyncio.run(run())


def test_fallback_rechecks_each_prepared_request_once():
    async def run():
        async def responder(_request, _body):
            if len(sent) == 1:
                return httpx2.Response(200, json=completion_payload('not-json'))
            return httpx2.Response(200, json=completion_payload())

        async with offline_model(responder) as (model, sent):
            formatter_calls = []
            inspections = []
            formatter_type = type(model.formatter)
            original_format = formatter_type.format
            from storyloop_harness.models import request_guard
            original_estimate = request_guard.estimate_request_envelope

            async def count_format(self, messages):
                formatter_calls.append(1)
                return await original_format(self, messages)

            def count_estimate(messages, tools, budget, *, envelope_options):
                inspections.append((messages, tools))
                return original_estimate(messages, tools, budget,
                                         envelope_options=envelope_options)

            with patch.object(formatter_type, 'format', count_format), \
                 patch.object(request_guard, 'estimate_request_envelope', count_estimate):
                await model([{'role': 'user', 'content': 'fallback'}], structured_model=Reply,
                            context_budget=ContextBudget())
            assert len(formatter_calls) == len(inspections) == len(sent) == 2
            for (messages, tools), body in zip(inspections, sent):
                assert messages == body['messages'] and tools == body['tools']
            assert sent[0]['tool_choice'] != sent[1]['tool_choice']
    asyncio.run(run())


def test_agent_scope_transient_retry_rechecks_request():
    async def run():
        async def responder(_request, _body):
            if len(sent) == 1:
                return httpx2.Response(429, json={'error': {'message': 'retry', 'type': 'rate_limit_error'}})
            return httpx2.Response(200, json=completion_payload())

        async with offline_model(responder) as (model, sent):
            model.retry_delay = 0
            model.max_retries = 1
            from storyloop_harness.models import request_guard
            original_estimate = request_guard.estimate_request_envelope
            checks = []

            def count_checks(messages, tools, budget, *, envelope_options):
                checks.append(1)
                return original_estimate(messages, tools, budget,
                                         envelope_options=envelope_options)

            with patch.object(request_guard, 'estimate_request_envelope', count_checks):
                await model([{'role': 'user', 'content': 'retry'}], structured_model=Reply,
                            context_budget=ContextBudget())
            assert len(sent) == 2
            assert len(checks) == 2
            assert sent[0] == sent[1]
    asyncio.run(run())


def test_concurrent_bounded_and_unbounded_calls_do_not_share_budget():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def responder(_request, body):
            if body.get('max_completion_tokens') == 100:
                entered.set()
                await release.wait()
            return httpx2.Response(200, json=completion_payload())

        async with offline_model(responder) as (model, sent):
            bounded = asyncio.create_task(model(
                [{'role': 'user', 'content': 'bounded'}], structured_model=Reply,
                context_budget=ContextBudget(output_tokens=100)))
            await entered.wait()
            await model([{'role': 'user', 'content': 'unbounded' * 20000}], structured_model=Reply)
            release.set()
            await bounded
            assert len(sent) == 2
            assert sent[0]['max_completion_tokens'] == 100
            assert 'max_completion_tokens' not in sent[1]
    asyncio.run(run())


def test_two_models_sharing_http_client_keep_distinct_budget_scopes():
    async def run():
        sent = []

        async def responder(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json=completion_payload())

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(responder))
        first = create_test_model('offline-test', http_client=client)
        second = create_test_model('offline-test', http_client=client)
        try:
            assert first.client.chat.completions is not second.client.chat.completions
            with pytest.raises(ContextBudgetExceeded):
                await first([{'role': 'user', 'content': 'x' * 20000}], structured_model=Reply,
                            context_budget=ContextBudget(context_window_tokens=1500, output_tokens=100,
                                                         safety_margin_tokens=0))
            await second([{'role': 'user', 'content': 'second'}], structured_model=Reply,
                         context_budget=ContextBudget(output_tokens=123))
            assert len(sent) == 1 and sent[0]['max_completion_tokens'] == 123
        finally:
            await first.client.close()
            await second.client.close()
            await client.aclose()
    asyncio.run(run())


def test_original_fields_mutated_after_check_cannot_change_sent_snapshot():
    async def run():
        from storyloop_harness.models import request_guard
        original_estimate = request_guard.estimate_request_envelope
        async with offline_model() as (model, sent):
            messages = [{'role': 'user', 'content': 'original'}]
            tools = [{'type': 'function', 'function': {'name': 'f',
                      'parameters': {'type': 'object', 'properties': {}}}}]
            extra = {'provider_flag': 'original'}

            def mutate_after_check(inspected_messages, inspected_tools, budget, *, envelope_options):
                report = original_estimate(inspected_messages, inspected_tools, budget,
                                           envelope_options=envelope_options)
                messages[0]['content'] = 'changed'
                tools[0]['function']['name'] = 'changed'
                extra['provider_flag'] = 'changed'
                return report

            with patch.object(request_guard, 'estimate_request_envelope', mutate_after_check):
                with model.client.chat.completions.scope(ContextBudget()):
                    await model.client.chat.completions.create(
                        model='offline-test', messages=messages, tools=tools,
                        extra_body=extra, stream=False, max_completion_tokens=4096)
            assert sent[0]['messages'][0]['content'] == 'original'
            assert sent[0]['tools'][0]['function']['name'] == 'f'
            assert sent[0]['provider_flag'] == 'original'
    asyncio.run(run())


def test_cancelled_call_resets_budget_before_later_unbounded_call():
    async def run():
        entered = asyncio.Event()

        async def responder(_request, body):
            if body.get('max_completion_tokens') == 100:
                entered.set()
                await asyncio.Future()
            return httpx2.Response(200, json=completion_payload())

        async with offline_model(responder) as (model, sent):
            task = asyncio.create_task(model.generate_structured_output(
                [{'role': 'user', 'content': 'cancel'}], Reply,
                context_budget=ContextBudget(output_tokens=100)))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await model([{'role': 'user', 'content': 'later' * 20000}], structured_model=Reply)
            assert len(sent) == 2 and 'max_completion_tokens' not in sent[1]
    asyncio.run(run())


def test_expanded_schema_and_preflight_cover_two_scene_shapes():
    from pathlib import Path
    from scene_fixtures import source_package
    from storyloop_harness.agents.scene_schema import scene_output_schema
    from storyloop_harness.agents.scene_prompt import scene_system_prompt, serialize_scene_request
    from storyloop_harness.context.budget import estimate_scene_request
    from storyloop_harness.world.scenario import ScenarioPackage

    async def run():
        packages = (ScenarioPackage.load(Path(__file__).resolve().parents[1] / 'examples' / 'freeform'),
                    source_package())
        for package in packages:
            arguments = {'prose': 'Synthetic harbor scene.'}
            if package.story_blueprint is not None:
                arguments['options'] = [{'label': str(i), 'input': str(i)} for i in range(3)]

            async def responder(_request, _body):
                return httpx2.Response(200, json=completion_payload(json.dumps(arguments)))

            async with offline_model(responder) as (model, sent):
                request = {'player_action': 'Synthetic "escaped" action.\n你好'}
                budget = ContextBudget()
                await model([{'role': 'system', 'content': scene_system_prompt(package)},
                             {'role': 'user', 'content': serialize_scene_request(request)}],
                            structured_model=scene_output_schema(package), context_budget=budget)
                actual = wire_estimate(sent[0], budget)
                preflight = estimate_scene_request(request, package, budget)
                assert preflight.total_tokens >= actual.total_tokens
                assert '$ref' not in json.dumps(sent[0]['tools'])
                assert '$defs' not in json.dumps(sent[0]['tools'])
    asyncio.run(run())


def test_nested_pydantic_schema_is_expanded_on_wire():
    async def run():
        async def responder(_request, _body):
            return httpx2.Response(200, json=completion_payload(
                '{"first":{"value":"a"},"second":{"value":"b"}}'))

        async with offline_model(responder) as (model, sent):
            await model([{'role': 'user', 'content': 'nested'}], structured_model=Expanded,
                        context_budget=ContextBudget())
            schema = json.dumps(sent[0]['tools'])
            assert '$ref' not in schema and '$defs' not in schema
            assert 'first' in schema and 'second' in schema
    asyncio.run(run())


def test_omitted_openai_fields_are_excluded_from_estimate_and_wire():
    from openai import Omit, NOT_GIVEN

    async def run():
        async with offline_model() as (model, sent):
            with model.client.chat.completions.scope(ContextBudget()):
                await model.client.chat.completions.create(
                    model='offline-test', messages=[{'role': 'user', 'content': 'omitted'}],
                    max_completion_tokens=4096, stream=False, temperature=Omit(), timeout=NOT_GIVEN)
            assert len(sent) == 1 and 'temperature' not in sent[0]
    asyncio.run(run())


def test_extra_body_omit_removes_configured_and_provider_keys_at_exact_boundary():
    from openai import Omit
    from storyloop_harness.models import request_guard

    async def run():
        inspected = []
        original_estimate = request_guard.estimate_request_envelope

        def capture(messages, tools, budget, *, envelope_options):
            inspected.append({'messages': messages, 'tools': tools, **envelope_options})
            return original_estimate(messages, tools, budget,
                                     envelope_options=envelope_options)

        async with offline_model() as (model, sent):
            messages = [{'role': 'user', 'content': 'omit override'}]
            extra = {'temperature': Omit(), 'provider_absent': Omit()}
            with patch.object(request_guard, 'estimate_request_envelope', capture):
                await model(messages, structured_model=Reply, context_budget=ContextBudget(),
                            extra_body=extra)
                assert len(sent) == 1
                assert 'temperature' not in sent[0] and 'provider_absent' not in sent[0]
                assert inspected[0]['messages'] == sent[0]['messages']
                assert inspected[0]['tools'] == sent[0]['tools']
                assert 'temperature' not in inspected[0]
                assert 'provider_absent' not in inspected[0]

                report = wire_estimate(sent[0], ContextBudget())
                exact = replace(ContextBudget(), context_window_tokens=report.total_tokens)
                await model(messages, structured_model=Reply, context_budget=exact,
                            extra_body=extra)
                assert sent[1] == sent[0]
                with pytest.raises(ContextBudgetExceeded) as error:
                    await model(messages, structured_model=Reply,
                                context_budget=replace(exact,
                                                       context_window_tokens=report.total_tokens - 1),
                                extra_body=extra)
                assert error.value.report.total_tokens == report.total_tokens
                assert len(sent) == 2
    asyncio.run(run())


@pytest.mark.parametrize('extra_body', [None, {'provider_flag': 'on'}])
def test_ordinary_provider_body_options_pass_through(extra_body):
    async def run():
        async with offline_model() as (model, sent):
            await model([{'role': 'user', 'content': 'provider option'}],
                        structured_model=Reply, context_budget=ContextBudget(),
                        extra_body=extra_body)
            assert len(sent) == 1
            assert sent[0].get('provider_flag') == (
                'on' if extra_body is not None else None)
    asyncio.run(run())


def test_failed_bounded_call_resets_scope_before_unbounded_call():
    async def run():
        async def responder(_request, _body):
            if len(sent) == 1:
                return httpx2.Response(500, json={'error': {'message': 'failure', 'type': 'server_error'}})
            return httpx2.Response(200, json=completion_payload())

        async with offline_model(responder) as (model, sent):
            model.max_retries = 0
            with pytest.raises(Exception):
                await model([{'role': 'user', 'content': 'bounded'}], structured_model=Reply,
                            context_budget=ContextBudget(output_tokens=100))
            await model([{'role': 'user', 'content': 'unbounded' * 20000}],
                        structured_model=Reply)
            assert len(sent) == 2
            assert 'max_completion_tokens' not in sent[1]
    asyncio.run(run())
