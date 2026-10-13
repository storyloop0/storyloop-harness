"""AgentScope model adaptation; callers supply configured credentials."""
from __future__ import annotations
import asyncio
from typing import Literal
from types import SimpleNamespace
from agentscope.message import Msg as AgentScopeMsg, SystemMsg, UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.tool import ToolChoice
from storyloop_harness.adapters.telemetry import NoopTelemetry, Telemetry
from storyloop_harness.models.usage import record_model_usage
from storyloop_harness.context import ContextBudget
from storyloop_harness.models.request_guard import GuardedCompletions
from storyloop_harness.models.attempt_tracking import AttemptTracker
from storyloop_harness.models.generation_budget import GenerationBudget, GenerationTimeout
from storyloop_harness.models.generation_control import controlled_generation, current_control, suspend_control

ToolChoicePolicy = Literal["native", "auto_only"]
StructuredOutputTransport = Literal["auto", "tool_call"]


class CompatibleOpenAIChatModel(OpenAIChatModel):
    """Adapt forced tool choices for endpoints that only accept auto/none."""

    def __init__(self, *args: object, tool_choice_policy: ToolChoicePolicy = "native",
                 structured_output_transport: StructuredOutputTransport = "auto",
                 telemetry: Telemetry | None = None, task: str = "model",
                 generation_budget: GenerationBudget | None = None, **kwargs: object) -> None:
        if structured_output_transport not in ("auto", "tool_call"):
            raise ValueError("unsupported structured_output_transport")
        if generation_budget is not None and not isinstance(generation_budget, GenerationBudget):
            raise TypeError("generation_budget must be GenerationBudget")
        self.generation_budget = generation_budget
        self.tool_choice_policy = tool_choice_policy
        self.telemetry = telemetry or NoopTelemetry()
        self.task = task
        super().__init__(*args, **kwargs)
        self._attempt_tracker = AttemptTracker(self.model, task)
        self._generation_control_key = object()
        # The copied public SDK client owns no distinct transport. Keep it for
        # this model's lifetime; only the original client owns close().
        self._controlled_client = (self.client.with_options(max_retries=0)
                                   if generation_budget is not None else None)
        controlled_resource = (self._controlled_client.chat.completions
                               if self._controlled_client is not None else None)
        self.client.chat.completions = GuardedCompletions(
            self.client.chat.completions, controlled_resource, self._generation_control_key,
        )

    @property
    def model_name(self) -> str:
        return self.model

    @staticmethod
    def _messages(messages: list) -> list[AgentScopeMsg]:
        converted = []
        for item in messages:
            if isinstance(item, AgentScopeMsg):
                converted.append(item)
            elif isinstance(item, dict):
                content = item.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(part.get("text", "") for part in content
                                        if isinstance(part, dict))
                if item.get("role") == "system":
                    converted.append(SystemMsg(name=item.get("name", "system"), content=content or ""))
                else:
                    converted.append(UserMsg(name=item.get("name", "user"), content=content or ""))
            else:
                raise TypeError("model messages must be AgentScope messages or OpenAI message dicts")
        return converted

    def _record_usage(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        record_model_usage(self.model, self.task, usage)

    async def _call_api(self, model_name: str, messages: list, tools=None,
                        tool_choice=None, **kwargs: object):
        if self.tool_choice_policy == "auto_only" and tool_choice is not None:
            if tool_choice.mode not in ("auto", "none"):
                tool_choice = ToolChoice(mode="auto")
        # The SDK propagates explicit kwargs separately for each attempt. Remove
        # this invocation's budget here; no shared model state survives a call,
        # retry, exception or cancellation, and it never reaches the client.
        budget = kwargs.pop("context_budget", None)
        if budget is not None:
            if not isinstance(budget, ContextBudget):
                raise TypeError("context_budget must be ContextBudget")
            if "max_tokens" in kwargs:
                raise ValueError("bounded calls require max_completion_tokens")
            cap = kwargs.get("max_completion_tokens", budget.output_tokens)
            if type(cap) is not int or cap != budget.output_tokens:
                raise ValueError("output cap must equal the context budget output allowance")
            critical = {"messages", "tools", "tool_choice", "max_tokens", "max_completion_tokens"}
            for extra in (self.extra_body, kwargs.get("extra_body")):
                if extra is not None and (not isinstance(extra, dict) or critical.intersection(extra)):
                    raise ValueError("bounded calls reject budget-critical extra_body overrides")
            kwargs["max_completion_tokens"] = budget.output_tokens
        with self.client.chat.completions.scope(budget):
            response = await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)
            return self._attempt_tracker.observe(response)

    async def _call_api_with_structured_output(self, *args: object, **kwargs: object):
        async with self._attempt_tracker.attempt():
            try:
                response = await super()._call_api_with_structured_output(*args, **kwargs)
            except asyncio.CancelledError:
                control = current_control(self._generation_control_key)
                if control is not None and control.owns_cancellation():
                    raise GenerationTimeout() from None
                raise
            control = current_control(self._generation_control_key)
            if control is not None:
                control.check_deadline()
            return response

    async def generate_structured_output(self, messages: list, structured_model: type, **kwargs: object):
        async with controlled_generation(self.generation_budget, self._generation_control_key):
            return await self._generate_structured_output(messages, structured_model, **kwargs)

    async def _generate_structured_output(self, messages: list, structured_model: type, **kwargs: object):
        messages = self._messages(messages)
        with self.telemetry.span(
            f"model:{self.task}", {"task": self.task,
                                  "structured_model": structured_model.__name__},
            kind="generation", model=self.model,
            input=messages if self.telemetry.capture_content else None,
        ) as generation:
            try:
                with self._attempt_tracker.invocation():
                    response = await super().generate_structured_output(
                        messages, structured_model, **kwargs,
                    )
            except Exception as error:
                generation.update(level="ERROR", status_message=type(error).__name__)
                raise
            self._record_usage(response)
            if response.usage is not None:
                generation.update(usage_details={"input": response.usage.input_tokens,
                                                 "output": response.usage.output_tokens})
            if self.telemetry.capture_content:
                generation.update(output=response.content)
            return response

    async def __call__(
        self,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        structured_model: type | None = None,
        **kwargs: object,
    ):
        if structured_model is not None:
            response = await self.generate_structured_output(messages, structured_model, **kwargs)
            return SimpleNamespace(metadata=response.content, usage=response.usage,
                                   content=response.content)
        if isinstance(tool_choice, str):
            tool_choice = ToolChoice(mode=tool_choice)
        if self.tool_choice_policy == "auto_only" and tool_choice is not None:
            if tool_choice.mode not in ("auto", "none"):
                tool_choice = ToolChoice(mode="auto")
        messages = self._messages(messages)
        with self.telemetry.span(
            f"model:{self.task}",
            {"task": self.task, "tool_choice": tool_choice,
             "available_tools": [item.get("function", {}).get("name", "") for item in tools or []],
             "structured_model": getattr(structured_model, "__name__", None)},
            kind="generation", model=self.model,
            input=messages if self.telemetry.capture_content else None,
        ) as generation:
            try:
                with suspend_control(self._generation_control_key):
                    response = await super().__call__(
                        messages, tools=tools, tool_choice=tool_choice,
                        **kwargs,
                    )
                self._record_usage(response)
            except Exception as error:
                generation.update(level="ERROR", status_message=type(error).__name__)
                raise
            if response.usage is not None:
                generation.update(usage_details={
                    "input": response.usage.input_tokens,
                    "output": response.usage.output_tokens,
                })
            if self.telemetry.capture_content:
                generation.update(output=response.content)
            return response
