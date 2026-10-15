"""Private observation of AgentScope's structured attempt boundary."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator, TYPE_CHECKING
from uuid import uuid4

from storyloop_harness.models.attempts import ModelAttempt, _active_attempts, AttemptCollector
from storyloop_harness.models.usage import ModelUsage, model_usage_from_response
from storyloop_harness.models.execution_control import current_execution_control
from storyloop_harness.models.execution_tokens import AttemptTokenUsage

if TYPE_CHECKING:
    from storyloop_harness.models.execution_control import ExecutionControl


@dataclass
class _Invocation:
    identifier: str
    collector: AttemptCollector | None
    ordinal: int = 0
    execution: ExecutionControl | None = None
    owner: asyncio.Task[object] | None = None


@dataclass
class _Attempt:
    invocation: _Invocation
    ordinal: int
    usage: ModelUsage | None = None
    usage_state: str = "missing"
    stream_complete: bool = False
    streams: list[tuple[object, object]] = field(default_factory=list)
    token_usage: AttemptTokenUsage | None = None


class AttemptTracker:
    def __init__(self, model: str, task: str) -> None:
        self.model = model
        self.task = task
        self._invocation: ContextVar[_Invocation | None] = ContextVar(
            f"storyloop_invocation_{id(self)}", default=None,
        )
        self._attempt: ContextVar[_Attempt | None] = ContextVar(
            f"storyloop_attempt_{id(self)}", default=None,
        )

    @contextmanager
    def invocation(self) -> Iterator[None]:
        collector = _active_attempts.get()
        execution = current_execution_control()
        enabled = execution is not None and execution.budget.max_reported_tokens is not None
        if collector is None and not enabled:
            yield
            return
        token = self._invocation.set(_Invocation(
            str(uuid4()), collector, execution=execution if enabled else None,
            owner=asyncio.current_task(),
        ))
        try:
            yield
        finally:
            self._invocation.reset(token)

    @asynccontextmanager
    async def attempt(self) -> AsyncIterator[None]:
        invocation = self._invocation.get()
        if invocation is None:
            yield
            return
        invocation.ordinal += 1
        current = _Attempt(invocation, invocation.ordinal)
        if invocation.execution is not None and invocation.owner is asyncio.current_task():
            current.token_usage = AttemptTokenUsage(invocation.execution.token_usage)
        token = self._attempt.set(current)
        primary: BaseException | None = None
        try:
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            cleanup_failure: BaseException | None = None
            try:
                for stream, close_source in current.streams:
                    try:
                        try:
                            await stream.aclose()
                        finally:
                            await close_source()
                    except BaseException as cleanup_error:
                        if primary is None:
                            primary = cleanup_error
                            cleanup_failure = cleanup_error
                if cleanup_failure is not None:
                    raise cleanup_failure
            finally:
                self._attempt.reset(token)
                if current.token_usage is not None:
                    current.token_usage.settle(complete=(
                        current.usage_state == 'reported' and current.stream_complete
                    ))
                if invocation.collector is not None:
                    outcome = ("cancelled" if isinstance(primary, asyncio.CancelledError)
                               else "failed" if primary is not None else "succeeded")
                    invocation.collector.records.append(ModelAttempt(
                        invocation.identifier, current.ordinal, self.model, self.task,
                        outcome, current.usage, current.usage_state,
                        current.stream_complete,
                        type(primary).__name__ if primary is not None else None,
                    ))

    def _snapshot(self, current: _Attempt, usage: object) -> None:
        if usage is None:
            return
        try:
            current.usage = model_usage_from_response(self.model, self.task, usage)
        except (AttributeError, TypeError, ValueError):
            current.usage = None
            current.usage_state = "invalid"
        else:
            current.usage_state = "reported"
            if current.token_usage is not None:
                current.token_usage.last_valid_usage = current.usage

    def current_token_attempt(self) -> AttemptTokenUsage | None:
        current = self._attempt.get()
        if current is None or current.invocation.owner is not asyncio.current_task():
            return None
        return current.token_usage

    @contextmanager
    def suspend_token_attempt(self) -> Iterator[None]:
        current = self._attempt.get()
        if current is None or current.token_usage is None:
            yield
            return
        token = self._attempt.set(None)
        try:
            yield
        finally:
            self._attempt.reset(token)

    def observe(self, response: object) -> object:
        current = self._attempt.get()
        if current is None:
            return response
        if hasattr(response, "__aiter__"):
            source = response
            source_closed = False

            async def close_source():
                nonlocal source_closed
                if source_closed:
                    return
                source_closed = True
                close = getattr(source, "aclose", None)
                if close is not None:
                    await close()

            async def observed():
                primary: BaseException | None = None
                try:
                    async for chunk in source:
                        self._snapshot(current, getattr(chunk, "usage", None))
                        yield chunk
                    current.stream_complete = True
                except BaseException as error:
                    if not isinstance(error, GeneratorExit):
                        primary = error
                    raise
                finally:
                    try:
                        await close_source()
                    except BaseException:
                        if primary is None:
                            raise

            wrapper = observed()
            current.streams.append((wrapper, close_source))
            return wrapper
        self._snapshot(current, getattr(response, "usage", None))
        current.stream_complete = True
        return response
