"""Invocation-local enforcement for structured completion sends and time."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import AsyncIterator, Iterator

from .generation_budget import GenerationBudget, GenerationRequestLimitExceeded, GenerationTimeout


@dataclass
class GenerationControl:
    budget: GenerationBudget
    deadline: float
    owner: object
    task: asyncio.Task[object] | None
    sends: int = 0
    timeout_scope: asyncio.Timeout | None = None
    initial_cancels: int = 0

    def check_deadline(self) -> None:
        if monotonic() >= self.deadline:
            raise GenerationTimeout()

    def owns_cancellation(self) -> bool:
        # asyncio.Timeout adds exactly one cancellation. Another outstanding
        # cancellation belongs to the caller and must keep its identity.
        return (self.timeout_scope is not None and self.timeout_scope.expired()
                and self.task is not None
                and self.task.cancelling() == self.initial_cancels + 1)

    def claim_send(self) -> None:
        self.check_deadline()
        if self.sends >= self.budget.max_requests:
            raise GenerationRequestLimitExceeded()
        self.sends += 1


_active_control: ContextVar[GenerationControl | None] = ContextVar(
    'storyloop_generation_control', default=None,
)


def current_control(owner: object) -> GenerationControl | None:
    control = _active_control.get()
    return (control if control is not None and control.owner is owner
            and control.task is asyncio.current_task() else None)


@contextmanager
def suspend_control(owner: object) -> Iterator[None]:
    if current_control(owner) is None:
        yield
        return
    token = _active_control.set(None)
    try:
        yield
    finally:
        _active_control.reset(token)


@asynccontextmanager
async def controlled_generation(budget: GenerationBudget | None,
                                owner: object) -> AsyncIterator[GenerationControl | None]:
    if budget is None:
        yield None
        return
    control = GenerationControl(budget, monotonic() + budget.timeout_seconds,
                                owner, asyncio.current_task())
    token = _active_control.set(control)
    try:
        timeout_scope = asyncio.timeout_at(control.deadline)
        try:
            async with timeout_scope:
                control.timeout_scope = timeout_scope
                control.initial_cancels = control.task.cancelling() if control.task else 0
                yield control
                control.check_deadline()
        except asyncio.CancelledError:
            # Both timeout callbacks can cancel the same task after a blocked
            # event loop resumes. Keep the inner deadline for the outer scope,
            # which is the one asyncio will turn into TimeoutError.
            from .execution_control import current_execution_control
            execution = current_execution_control()
            if (execution is not None and timeout_scope.expired()
                    and execution.timeout_scope is not None
                    and execution.timeout_scope.expired()
                    and control.deadline < execution.deadline):
                execution.expired_generation_deadline = control.deadline
            raise
        except TimeoutError as error:
            if not timeout_scope.expired() and not isinstance(error, GenerationTimeout):
                raise
            from .execution_control import check_deadlines, current_execution_control
            check_deadlines(current_execution_control(), control)
            raise
    finally:
        _active_control.reset(token)
