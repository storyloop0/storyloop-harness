"""Story-first generation with a small, optional persistence sidecar."""

from __future__ import annotations

from copy import deepcopy

from storyloop_harness.adapters.agentscope_message import Msg
from storyloop_harness.ports import ModelPort
from storyloop_harness.agents import scene_schema
from storyloop_harness.agents.scene_prompt import scene_system_prompt, serialize_scene_request
from storyloop_harness.context import ContextProjection, ContextBudgetExceeded
from storyloop_harness.context.budget import estimate_scene_request, enforce_window
from storyloop_harness.context.metrics import record_projection_metrics
from storyloop_harness.core.token_budget import estimate_tokens
from storyloop_harness.adapters.telemetry import NoopTelemetry, Telemetry, session_id_for_game
from storyloop_harness.agents.openai_formatter import ThinkingSafeOpenAIChatFormatter
from storyloop_harness.world.scenario import ScenarioPackage


class SingleSceneGenerator:
    def __init__(self, model: ModelPort, package: ScenarioPackage,
                 telemetry: Telemetry | None = None) -> None:
        self.model = model
        self.package = package
        self.telemetry = telemetry or NoopTelemetry()
        self.formatter = ThinkingSafeOpenAIChatFormatter()

    async def generate(self, game_id: str, context: ContextProjection) -> scene_schema.SceneTurn:
        # Freeze the current request before the first await; detached inspection
        # dictionaries are mutable and their cached report is not authoritative.
        request = deepcopy(context.request)
        report = estimate_scene_request(request, self.package, context.budget,
                                        decisions=context.report.decisions)
        system = scene_system_prompt(self.package)
        schema = scene_schema.scene_output_schema(self.package)
        with self.telemetry.span(
            "single-scene-generation",
            {"game_id": game_id, "candidate_actor_ids": list(context.focus_actor_ids)},
            kind="agent", input=request if self.telemetry.capture_content else None,
            session_id=session_id_for_game(game_id, self.package.package_id),
        ) as span:
            record_projection_metrics(span, report)
            if estimate_tokens(serialize_scene_request(request)) > context.budget.max_material_tokens:
                raise ContextBudgetExceeded(report)
            enforce_window(report)
            prompt = await self.formatter.format(msgs=[
                Msg('system', system, 'system'),
                Msg('player', serialize_scene_request(request), 'user'),
            ])
            response = await self.model(prompt, structured_model=schema,
                context_budget=context.budget, max_completion_tokens=context.budget.output_tokens)
            result = schema.model_validate(response.metadata)
            result = result.model_copy(update={
                "prose": result.prose.replace("\\r\\n", "\n").replace("\\n", "\n").strip(),
                "replies": [item.model_copy(update={
                    "speech": item.speech.replace("\\r\\n", "\n").replace("\\n", "\n").strip(),
                }) for item in result.replies],
            })
            span.metric("story.single_scene_model_calls", 1.0)
            if self.telemetry.capture_content:
                span.update(output=result.model_dump())
            return result.for_storage()
