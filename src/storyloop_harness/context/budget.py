"""Conservative request accounting shared by preflight and SDK wire guard."""
import json
from agentscope.model import OpenAIChatModel
from storyloop_harness.core.token_budget import estimate_tokens
from .types import ContextBudget, ContextBudgetExceeded, MaterialDecision, ProjectionReport

STRUCTURED_TRANSPORT_ALLOWANCE = 512

def serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)

def estimate_request_envelope(messages: list, tools: list | None, budget: ContextBudget,
                              *, envelope_options: dict | None = None,
                              decisions: tuple[MaterialDecision, ...] = (),
                              scope: str = 'ordinary-single-scene-wire') -> ProjectionReport:
    system = [message for message in messages if message.get('role') == 'system']
    material = [message for message in messages if message.get('role') != 'system']
    system_tokens = estimate_tokens(serialize(system)) if system else 0
    material_tokens = estimate_tokens(serialize(material)) if material else 0
    schema_tokens = estimate_tokens(serialize(tools)) if tools else 0
    envelope = {**(envelope_options or {}), 'messages': messages}
    if tools:
        envelope['tools'] = tools
    framing = max(0, estimate_tokens(serialize(envelope)) - system_tokens - material_tokens - schema_tokens)
    return ProjectionReport(system_tokens, material_tokens, schema_tokens, framing,
                            budget.output_tokens, budget.safety_margin_tokens,
                            budget.context_window_tokens, decisions, scope)

def enforce_window(report: ProjectionReport) -> None:
    if report.total_tokens > report.context_window_tokens:
        raise ContextBudgetExceeded(report)

def estimate_scene_request(request: dict[str, object], package, budget: ContextBudget,
                           *, decisions: tuple[MaterialDecision, ...] = (),
                           scope: str = 'ordinary-single-scene') -> ProjectionReport:
    from storyloop_harness.agents.scene_prompt import scene_system_prompt, serialize_scene_request
    from storyloop_harness.agents.scene_schema import scene_output_schema
    messages = [
        {'role': 'system', 'name': 'system', 'content': scene_system_prompt(package)},
        {'role': 'user', 'name': 'player', 'content': serialize_scene_request(request)},
    ]
    tools = OpenAIChatModel._flatten_tool_schemas([{'type': 'function', 'function': {
        'name': 'generate_structured_output',
        'description': 'Call this function to generate structured output required by the user.',
        'parameters': scene_output_schema(package).model_json_schema(),
    }}])
    report = estimate_request_envelope(messages, tools, budget, envelope_options={
        'tool_choice': {'type': 'function', 'function': {'name': 'generate_structured_output'}},
        'max_completion_tokens': budget.output_tokens,
    }, decisions=decisions, scope=scope)
    from dataclasses import replace
    return replace(report, transport_tokens=report.transport_tokens + STRUCTURED_TRANSPORT_ALLOWANCE)
