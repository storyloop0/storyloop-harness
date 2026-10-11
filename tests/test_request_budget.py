import pytest
from storyloop_harness.context import ContextBudget

@pytest.mark.parametrize('field,value', [('context_window_tokens', True), ('output_tokens', 0), ('safety_margin_tokens', -1), ('max_material_tokens', 1.5)])
def test_budget_rejects_non_integer_and_invalid_limits(field, value):
    with pytest.raises(ValueError):
        ContextBudget(**{field: value})

def test_budget_defaults_and_reserve_validation():
    budget = ContextBudget()
    assert (budget.context_window_tokens, budget.output_tokens, budget.safety_margin_tokens, budget.max_material_tokens) == (65536, 4096, 256, 16000)
    with pytest.raises(ValueError):
        ContextBudget(context_window_tokens=4352)

import asyncio
import json
from unittest.mock import AsyncMock
from pydantic import BaseModel
from openai.types.chat import ChatCompletion
from model_helpers import create_test_model
from storyloop_harness.context import ContextBudgetExceeded

class Nested(BaseModel):
    value: str

class Expanded(BaseModel):
    first: Nested
    second: Nested

def completion():
    return ChatCompletion.model_validate({
        'id': 'offline', 'created': 0, 'model': 'offline-test', 'object': 'chat.completion',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'tool', 'type': 'function',
                'function': {'name': 'generate_structured_output', 'arguments': '{"first":{"value":"a"},"second":{"value":"b"}}'}}]}}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})

def test_sdk_cap_expansion_and_internal_kwarg_removed():
    async def run():
        model = create_test_model('offline-test')
        model.parameters.max_tokens = 9000
        model.client.chat.completions.create = AsyncMock(return_value=completion())
        budget = ContextBudget(output_tokens=123)
        await model([{'role': 'user', 'content': 'synthetic'}], structured_model=Expanded,
                    context_budget=budget, max_completion_tokens=123)
        wire = model.client.chat.completions.create.await_args.kwargs
        assert wire['max_completion_tokens'] == 123
        assert 'max_tokens' not in wire and 'context_budget' not in wire
        assert '<system-reminder>' in json.dumps(wire['messages'])
        assert '$ref' not in json.dumps(wire['tools']) and '$defs' not in json.dumps(wire['tools'])
    asyncio.run(run())

def test_sdk_rejects_oversized_request_before_client_and_resets_budget():
    async def run():
        model = create_test_model('offline-test')
        model.client.chat.completions.create = AsyncMock(return_value=completion())
        budget = ContextBudget(context_window_tokens=1500, output_tokens=100, safety_margin_tokens=0)
        with pytest.raises(ContextBudgetExceeded) as error:
            await model([{'role': 'user', 'content': 'synthetic-marker' * 200}], structured_model=Expanded,
                        context_budget=budget, max_completion_tokens=100)
        assert model.client.chat.completions.create.await_count == 0
        assert 'synthetic-marker' not in str(error.value)
        assert 'synthetic-marker' not in json.dumps(error.value.report.to_dict())
        await model([{'role': 'user', 'content': 'synthetic'}], structured_model=Expanded)
        assert model.client.chat.completions.create.await_count == 1
    asyncio.run(run())

@pytest.mark.parametrize('field', ['messages', 'tools', 'tool_choice', 'max_tokens', 'max_completion_tokens'])
def test_sdk_rejects_budget_critical_extra_body(field):
    async def run():
        model = create_test_model('offline-test')
        model.extra_body = {field: 'synthetic'}
        model.client.chat.completions.create = AsyncMock(return_value=completion())
        with pytest.raises(ValueError, match='budget-critical'):
            await model([{'role': 'user', 'content': 'synthetic'}], structured_model=Expanded,
                        context_budget=ContextBudget(), max_completion_tokens=4096)
        assert model.client.chat.completions.create.await_count == 0
    asyncio.run(run())

def test_canonical_source_and_freeform_schema_and_accounting():
    from pathlib import Path
    from storyloop_harness.agents.scene_schema import scene_output_schema
    from storyloop_harness.agents.scene_prompt import scene_system_prompt
    from storyloop_harness.context.budget import estimate_scene_request
    from storyloop_harness.world.scenario import ScenarioPackage
    package = ScenarioPackage.load(Path(__file__).resolve().parents[1] / 'examples' / 'freeform')
    assert scene_output_schema(package).__name__ == 'NarrativeTurn'
    assert scene_system_prompt(package).endswith('输出 NarrativeTurn。')
    report = estimate_scene_request({'player_action': 'synthetic'}, package, ContextBudget())
    assert report.system_tokens > 0 and report.material_tokens > 0 and report.schema_tokens > 0
    assert report.transport_tokens >= 512
    assert report.total_tokens == sum([report.system_tokens, report.material_tokens, report.schema_tokens,
                                      report.transport_tokens, 4096, 256])
    assert report.remaining_tokens == 65536 - report.total_tokens


def test_wire_estimate_boundary_and_concurrent_budgets():
    from dataclasses import replace
    from storyloop_harness.context.budget import estimate_request_envelope
    async def run():
        model = create_test_model('offline-test')
        captured = []
        entered = asyncio.Event()
        release = asyncio.Event()
        async def capture(**kwargs):
            captured.append(kwargs)
            entered.set()
            await release.wait()
            return completion()
        model.client.chat.completions.create = AsyncMock(side_effect=capture)
        messages = [{'role': 'user', 'content': 'synthetic'}]
        first = asyncio.create_task(model(messages, structured_model=Expanded,
            context_budget=ContextBudget(output_tokens=101), max_completion_tokens=101))
        await entered.wait()
        with pytest.raises(ContextBudgetExceeded):
            await model([{'role': 'user', 'content': 'synthetic' * 2000}], structured_model=Expanded,
                context_budget=ContextBudget(context_window_tokens=1500, output_tokens=202),
                max_completion_tokens=202)
        release.set()
        await first
        assert len(captured) == 1 and captured[0]['max_completion_tokens'] == 101
        wire = captured[0]
        report = estimate_request_envelope(wire['messages'], wire['tools'], ContextBudget(output_tokens=101),
            envelope_options={k: v for k, v in wire.items() if k not in ('messages', 'tools')})
        exact = ContextBudget(context_window_tokens=report.total_tokens, output_tokens=101)
        await model(messages, structured_model=Expanded, context_budget=exact, max_completion_tokens=101)
        too_small = replace(exact, context_window_tokens=exact.context_window_tokens - 1)
        with pytest.raises(ContextBudgetExceeded) as error:
            await model(messages, structured_model=Expanded, context_budget=too_small, max_completion_tokens=101)
        assert error.value.report.total_tokens == report.total_tokens
        assert len(captured) == 2
    asyncio.run(run())

def test_streaming_sdk_option_is_included_at_final_wire_boundary():
    from dataclasses import replace
    from storyloop_harness.context.budget import estimate_request_envelope

    async def run():
        model = create_test_model('offline-test')
        model.stream = True
        model._parse_stream_response = lambda start, response: 'parsed offline stream'
        model.client.chat.completions.create = AsyncMock(return_value=object())
        messages = model._messages([{'role': 'user', 'content': 'synthetic streaming request'}])

        await model._call_api(model.model, messages, context_budget=ContextBudget(),
                              max_completion_tokens=4096)
        wire = model.client.chat.completions.create.await_args.kwargs
        assert wire['stream_options'] == {'include_usage': True}
        actual = estimate_request_envelope(
            wire['messages'], wire.get('tools'), ContextBudget(),
            envelope_options={key: value for key, value in wire.items()
                              if key not in ('messages', 'tools')},
        )

        at_boundary = replace(ContextBudget(), context_window_tokens=actual.total_tokens)
        await model._call_api(model.model, messages, context_budget=at_boundary,
                              max_completion_tokens=4096)
        assert model.client.chat.completions.create.await_count == 2

        below_boundary = replace(at_boundary, context_window_tokens=actual.total_tokens - 1)
        with pytest.raises(ContextBudgetExceeded):
            await model._call_api(model.model, messages, context_budget=below_boundary,
                                  max_completion_tokens=4096)
        assert model.client.chat.completions.create.await_count == 2

    asyncio.run(run())

def test_streaming_extra_body_override_matches_effective_openai_json_body():
    from dataclasses import replace
    from storyloop_harness.context.budget import estimate_request_envelope

    async def run():
        model = create_test_model('offline-test')
        model.stream = True
        model.parameters.parallel_tool_calls = False
        model._parse_stream_response = lambda start, response: 'parsed offline stream'
        model.client.chat.completions.create = AsyncMock(return_value=object())
        messages = model._messages([{'role': 'user', 'content': 'synthetic override request'}])
        tools = [{'type': 'function', 'function': {'name': 'synthetic_tool',
                  'description': 'Synthetic tool.', 'parameters': {'type': 'object', 'properties': {}}}}]
        extra_body = {'stream_options': {'include_usage': False,
                                         'synthetic_padding': 'x' * 2000}}

        await model._call_api(model.model, messages, tools=tools, context_budget=ContextBudget(),
                              max_completion_tokens=4096, parallel_tool_calls=True,
                              extra_body=extra_body)
        wire = model.client.chat.completions.create.await_args.kwargs
        assert wire['stream_options'] == {'include_usage': True}
        assert wire['parallel_tool_calls'] is False
        assert wire['extra_body'] == extra_body
        # OpenAI merges extra_body into the request JSON with later key
        # precedence, so its stream_options replaces AgentScope's default.
        effective_body = {key: value for key, value in wire.items()
                          if key not in ('messages', 'tools', 'extra_body')}
        effective_body.update(wire['extra_body'])
        actual = estimate_request_envelope(
            wire['messages'], wire.get('tools'), ContextBudget(),
            envelope_options=effective_body,
        )

        at_boundary = replace(ContextBudget(), context_window_tokens=actual.total_tokens)
        await model._call_api(model.model, messages, tools=tools, context_budget=at_boundary,
                              max_completion_tokens=4096, parallel_tool_calls=True,
                              extra_body=extra_body)
        assert model.client.chat.completions.create.await_count == 2

        below_boundary = replace(at_boundary, context_window_tokens=actual.total_tokens - 1)
        with pytest.raises(ContextBudgetExceeded) as error:
            await model._call_api(model.model, messages, tools=tools, context_budget=below_boundary,
                                  max_completion_tokens=4096, parallel_tool_calls=True,
                                  extra_body=extra_body)
        assert error.value.report.total_tokens == actual.total_tokens
        assert model.client.chat.completions.create.await_count == 2

    asyncio.run(run())

@pytest.mark.parametrize('extra_body', [None, {'enable_thinking': False}])
def test_sdk_allows_ordinary_provider_options(extra_body):
    async def run():
        model = create_test_model('offline-test')
        model.client.chat.completions.create = AsyncMock(return_value=completion())
        await model([{'role': 'user', 'content': 'synthetic'}], structured_model=Expanded,
                    context_budget=ContextBudget(), extra_body=extra_body)
        assert model.client.chat.completions.create.await_args.kwargs['max_completion_tokens'] == 4096
        assert model.client.chat.completions.create.await_args.kwargs['extra_body'] == extra_body
    asyncio.run(run())

def test_concurrent_unbounded_request_does_not_inherit_budget():
    async def run():
        model = create_test_model('offline-test')
        entered, release = asyncio.Event(), asyncio.Event()
        captured = []
        async def capture(**kwargs):
            captured.append(kwargs)
            entered.set()
            if kwargs.get('max_completion_tokens') == 100:
                await release.wait()
            return completion()
        model.client.chat.completions.create = AsyncMock(side_effect=capture)
        bounded = asyncio.create_task(model([{'role': 'user', 'content': 'synthetic'}],
            structured_model=Expanded, context_budget=ContextBudget(output_tokens=100)))
        await entered.wait()
        await model([{'role': 'user', 'content': 'synthetic' * 20000}], structured_model=Expanded)
        release.set()
        await bounded
        assert len(captured) == 2
        assert 'max_completion_tokens' not in captured[1]
    asyncio.run(run())

def test_preflight_covers_captured_scene_wire_for_both_schemas():
    from pathlib import Path
    from scene_fixtures import source_package
    from storyloop_harness.agents.scene_schema import scene_output_schema
    from storyloop_harness.agents.scene_prompt import scene_system_prompt, serialize_scene_request
    from storyloop_harness.context.budget import estimate_scene_request, estimate_request_envelope
    from storyloop_harness.world.scenario import ScenarioPackage
    async def run():
        for package in (ScenarioPackage.load(Path(__file__).resolve().parents[1] / 'examples' / 'freeform'), source_package()):
            model = create_test_model('offline-test')
            schema = scene_output_schema(package)
            reply = completion().model_dump()
            arguments = {'prose': 'Synthetic harbor scene.'}
            if package.story_blueprint is not None:
                arguments['options'] = [{'label': str(i), 'input': str(i)} for i in range(3)]
            reply['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = json.dumps(arguments)
            model.client.chat.completions.create = AsyncMock(return_value=ChatCompletion.model_validate(reply))
            request = {'player_action': 'Synthetic "escaped" action.\n你好'}
            budget = ContextBudget()
            await model([{'role': 'system', 'content': scene_system_prompt(package)},
                         {'role': 'user', 'content': serialize_scene_request(request)}],
                        structured_model=schema, context_budget=budget)
            wire = model.client.chat.completions.create.await_args.kwargs
            actual = estimate_request_envelope(wire['messages'], wire['tools'], budget,
                envelope_options={k: v for k, v in wire.items() if k not in ('messages', 'tools')})
            preflight = estimate_scene_request(request, package, budget)
            assert preflight.total_tokens >= actual.total_tokens
            assert '$ref' not in json.dumps(wire['tools'])
    asyncio.run(run())

def test_budget_report_is_immutable_and_serializable_without_prompt_content():
    from dataclasses import FrozenInstanceError
    from storyloop_harness.context import MaterialDecision, ProjectionReport
    decision = MaterialDecision('history', 'event-1', 'player_history', True, 'recent', 10, 8,
                                owner='player', state_version=2, truncated=True)
    report = ProjectionReport(10, 20, 30, 40, 50, 60, 300, (decision,))
    assert report.total_tokens == 210 and report.remaining_tokens == 90
    assert json.loads(json.dumps(report.to_dict()))['decisions'][0]['source_id'] == 'event-1'
    with pytest.raises(FrozenInstanceError):
        report.output_tokens = 100

@pytest.mark.parametrize('kwargs', [{'max_tokens': 100}, {'max_completion_tokens': 100}, {'max_completion_tokens': True}])
def test_bounded_call_rejects_conflicting_output_caps(kwargs):
    async def run():
        model = create_test_model('offline-test')
        model.client.chat.completions.create = AsyncMock(return_value=completion())
        with pytest.raises(ValueError):
            await model([{'role': 'user', 'content': 'synthetic'}], structured_model=Expanded,
                        context_budget=ContextBudget(), **kwargs)
        assert model.client.chat.completions.create.await_count == 0
    asyncio.run(run())

@pytest.mark.parametrize('field', ['messages', 'tools', 'tool_choice', 'max_tokens', 'max_completion_tokens'])
def test_bounded_call_rejects_per_call_extra_body_override(field):
    async def run():
        model = create_test_model('offline-test')
        model.client.chat.completions.create = AsyncMock(return_value=completion())
        with pytest.raises(ValueError, match='budget-critical'):
            await model([{'role': 'user', 'content': 'synthetic'}], structured_model=Expanded,
                        context_budget=ContextBudget(), extra_body={field: 'synthetic'})
        assert model.client.chat.completions.create.await_count == 0
    asyncio.run(run())

def test_cancelled_bounded_call_does_not_leave_budget_on_model():
    async def run():
        model = create_test_model('offline-test')
        entered = asyncio.Event()
        async def capture(**kwargs):
            entered.set()
            await asyncio.Future()
        model.client.chat.completions.create = AsyncMock(side_effect=capture)
        task = asyncio.create_task(model([{'role': 'user', 'content': 'synthetic'}],
            structured_model=Expanded, context_budget=ContextBudget(output_tokens=100)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        model.client.chat.completions.create = AsyncMock(return_value=completion())
        await model([{'role': 'user', 'content': 'synthetic' * 20000}], structured_model=Expanded)
        assert 'max_completion_tokens' not in model.client.chat.completions.create.await_args.kwargs
    asyncio.run(run())
