"""Per-model guard for the final OpenAI chat completion request."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, Iterator

from openai import Omit, NOT_GIVEN

from storyloop_harness.context import ContextBudget
from storyloop_harness.context.budget import estimate_request_envelope, enforce_window
from storyloop_harness.models.generation_control import current_control
from storyloop_harness.models.execution_control import current_execution_control, admit_send


_TRANSPORT_FIELDS = frozenset({"timeout", "extra_headers", "extra_query"})
_CRITICAL_FIELDS = frozenset({"messages", "tools", "tool_choice", "max_tokens", "max_completion_tokens"})


class GuardedCompletions:
    """Check a detached body snapshot, then send that same snapshot."""

    def __init__(self, resource: Any, controlled_resource: Any = None,
                 control_owner: object = None) -> None:
        self._resource = resource
        self._controlled_resource = controlled_resource
        self._control_owner = control_owner
        self._budget: ContextVar[ContextBudget | None] = ContextVar(
            "storyloop_request_budget", default=None,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resource, name)

    @contextmanager
    def scope(self, budget: ContextBudget | None) -> Iterator[None]:
        token = self._budget.set(budget)
        try:
            yield
        finally:
            self._budget.reset(token)

    async def create(self, **kwargs: Any) -> Any:
        budget = self._budget.get()
        execution = current_execution_control()
        control = current_control(self._control_owner)
        if budget is None and control is None and execution is None:
            return await self._resource.create(**kwargs)

        # Transport arguments are not JSON body fields and can hold SDK
        # objects (notably Timeout) that cannot be copied or tokenized.
        snapshot = {
            key: value if key in _TRANSPORT_FIELDS else deepcopy(value)
            for key, value in kwargs.items()
        }
        body = {
            key: value for key, value in snapshot.items()
            if key not in _TRANSPORT_FIELDS and not isinstance(value, (Omit, type(NOT_GIVEN)))
        }
        extra_body = body.pop("extra_body", None)
        if extra_body is not None:
            if not isinstance(extra_body, dict) or _CRITICAL_FIELDS.intersection(extra_body):
                raise ValueError("bounded calls reject budget-critical extra_body overrides")
            # OpenAI removes NotGiven from extra_body before its merge; an
            # absent override must leave an existing body value intact.
            body.update({key: value for key, value in extra_body.items()
                         if not isinstance(value, type(NOT_GIVEN))})
            # Omit removes a key after the merge, including an existing one.
            body = {key: value for key, value in body.items() if not isinstance(value, Omit)}
        if budget is not None:
            messages = body.pop("messages")
            tools = body.pop("tools", None)
            report = estimate_request_envelope(messages, tools, budget, envelope_options=body)
            enforce_window(report)
        if control is not None or execution is not None:
            admit_send(execution, control)
            return await self._controlled_resource.create(**snapshot)
        return await self._resource.create(**snapshot)
