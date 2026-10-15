"""Task-owned turn-wide execution scope and final-send admission."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import AsyncIterator

from .execution_budget import (
    ExecutionBudget, ExecutionRequestLimitExceeded, ExecutionTimeout,
    ExecutionTokenLimitExceeded, ExecutionUsageUnavailable,
)
from .execution_tokens import AttemptTokenUsage, ReportedTokenLedger
from .generation_budget import GenerationRequestLimitExceeded, GenerationTimeout
from .generation_control import GenerationControl


@dataclass
class ExecutionControl:
    budget: ExecutionBudget
    deadline: float
    task: asyncio.Task[object] | None
    sends: int = 0
    timeout_scope: asyncio.Timeout | None = None
    initial_cancels: int = 0
    expired_generation_deadline: float | None = None
    token_usage: ReportedTokenLedger = field(default_factory=ReportedTokenLedger)

    def check_deadline(self) -> None:
        if monotonic() >= self.deadline:
            raise ExecutionTimeout()

    def owns_cancellation(self) -> bool:
        return (self.timeout_scope is not None and self.timeout_scope.expired()
                and self.task is not None
                and self.task.cancelling() == self.initial_cancels + 1)


_active_execution: ContextVar[ExecutionControl | None] = ContextVar(
    'storyloop_execution_control', default=None,
)


def current_execution_control() -> ExecutionControl | None:
    control = _active_execution.get()
    if control is not None and control.task is not asyncio.current_task():
        raise ExecutionRequestLimitExceeded()
    return control


def check_deadlines(execution: ExecutionControl | None,
                    generation: GenerationControl | None) -> None:
    now = monotonic()
    if execution is not None and now >= execution.deadline and (
        generation is None or execution.deadline <= generation.deadline
    ):
        raise ExecutionTimeout()
    if generation is not None and now >= generation.deadline:
        raise GenerationTimeout()
    if execution is not None and now >= execution.deadline:
        raise ExecutionTimeout()


def admit_send(execution: ExecutionControl | None,
               generation: GenerationControl | None,
               attempt: AttemptTokenUsage | None = None) -> None:
    """Validate both policies, then claim both without yielding control."""
    check_deadlines(execution, generation)
    if execution is not None and execution.sends >= execution.budget.max_requests:
        raise ExecutionRequestLimitExceeded()
    if generation is not None and generation.sends >= generation.budget.max_requests:
        raise GenerationRequestLimitExceeded()
    if execution is not None and execution.budget.max_reported_tokens is not None:
        # A pending send belongs only to its own receipt; another attempt cannot borrow it.
        if (attempt is None or attempt.ledger is not execution.token_usage
                or attempt.settled or not execution.token_usage.complete
                or execution.token_usage.pending_attempts != int(attempt.pending)):
            raise ExecutionUsageUnavailable()
        if execution.token_usage.known_tokens >= execution.budget.max_reported_tokens:
            raise ExecutionTokenLimitExceeded()
    if execution is not None:
        execution.sends += 1
    if generation is not None:
        generation.sends += 1
    if (execution is not None and attempt is not None
            and attempt.ledger is execution.token_usage and not attempt.settled):
        if execution.budget.max_reported_tokens is not None and not attempt.pending:
            attempt.pending = True
            execution.token_usage.pending_attempts += 1
        attempt.sends += 1


@asynccontextmanager
async def execution_budget_scope(budget: ExecutionBudget) -> AsyncIterator[ExecutionControl]:
    if not isinstance(budget, ExecutionBudget):
        raise TypeError('budget must be ExecutionBudget')
    existing = current_execution_control()
    if existing is not None:
        if existing.budget != budget:
            raise ValueError('nested execution budget must match the active budget')
        check_deadlines(existing, None)
        yield existing
        check_deadlines(existing, None)
        return
    task = asyncio.current_task()
    control = ExecutionControl(budget, monotonic() + budget.timeout_seconds, task)
    token = _active_execution.set(control)
    try:
        timeout_scope = asyncio.timeout_at(control.deadline)
        try:
            async with timeout_scope:
                control.timeout_scope = timeout_scope
                control.initial_cancels = task.cancelling() if task else 0
                yield control
                check_deadlines(control, None)
        except TimeoutError as error:
            if isinstance(error, (ExecutionTimeout, GenerationTimeout)) or not timeout_scope.expired():
                raise
            if (control.expired_generation_deadline is not None
                    and control.expired_generation_deadline < control.deadline):
                raise GenerationTimeout() from None
            raise ExecutionTimeout() from None
    finally:
        _active_execution.reset(token)
