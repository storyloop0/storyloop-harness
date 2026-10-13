"""Public immutable observations of logical structured model attempts."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Literal

from storyloop_harness.models.usage import ModelUsage


@dataclass(frozen=True)
class ModelAttempt:
    invocation_id: str
    ordinal: int
    model: str
    task: str
    outcome: Literal["succeeded", "failed", "cancelled"]
    usage: ModelUsage | None
    usage_state: Literal["reported", "missing", "invalid"]
    stream_complete: bool
    error_type: str | None

    def __post_init__(self) -> None:
        if (not isinstance(self.invocation_id, str) or not self.invocation_id
                or type(self.ordinal) is not int or self.ordinal < 1
                or not isinstance(self.model, str) or not self.model
                or not isinstance(self.task, str) or not self.task
                or self.outcome not in ("succeeded", "failed", "cancelled")
                or self.usage_state not in ("reported", "missing", "invalid")
                or (self.usage is not None) != (self.usage_state == "reported")
                or (self.usage is not None and
                    (self.usage.model != self.model or self.usage.task != self.task))
                or type(self.stream_complete) is not bool
                or (self.outcome == "succeeded") != (self.error_type is None)
                or (self.error_type is not None and
                    (not isinstance(self.error_type, str) or not self.error_type))):
            raise ValueError("invalid structured model attempt")


@dataclass
class AttemptCollector:
    records: list[ModelAttempt] = field(default_factory=list)


_active_attempts: ContextVar[AttemptCollector | None] = ContextVar(
    "storyloop_model_attempts", default=None,
)


@contextmanager
def collect_attempts() -> Iterator[AttemptCollector]:
    collector = AttemptCollector()
    token = _active_attempts.set(collector)
    try:
        yield collector
    finally:
        _active_attempts.reset(token)
