"""Private observation of AgentScope's structured attempt boundary."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator
from uuid import uuid4

from storyloop_harness.models.attempts import ModelAttempt, _active_attempts, AttemptCollector
from storyloop_harness.models.usage import ModelUsage, model_usage_from_response


@dataclass
class _Invocation:
    identifier: str
    collector: AttemptCollector | None
    ordinal: int = 0


@dataclass
class _Attempt:
    invocation: _Invocation
    ordinal: int
    usage: ModelUsage | None = None
    usage_state: str = "missing"
    stream_complete: bool = False
    streams: list[object] = field(default_factory=list)


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
        token = self._invocation.set(_Invocation(str(uuid4()), _active_attempts.get()))
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
        token = self._attempt.set(current)
        primary: BaseException | None = None
        try:
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                for stream in current.streams:
                    try:
                        await stream.aclose()
                    except BaseException as cleanup_error:
                        if primary is None:
                            primary = cleanup_error
                            raise
            finally:
                self._attempt.reset(token)
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

    def observe(self, response: object) -> object:
        current = self._attempt.get()
        if current is None:
            return response
        if hasattr(response, "__aiter__"):
            source = response

            async def observed():
                primary: BaseException | None = None
                try:
                    async for chunk in source:
                        self._snapshot(current, getattr(chunk, "usage", None))
                        yield chunk
                    current.stream_complete = True
                except BaseException as error:
                    primary = error
                    raise
                finally:
                    close = getattr(source, "aclose", None)
                    if close is not None:
                        try:
                            await close()
                        except BaseException:
                            if primary is None:
                                raise

            wrapper = observed()
            current.streams.append(wrapper)
            return wrapper
        self._snapshot(current, getattr(response, "usage", None))
        current.stream_complete = True
        return response
