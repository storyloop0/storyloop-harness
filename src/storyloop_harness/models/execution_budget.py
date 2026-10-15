"""Optional, turn-wide completion send and elapsed-time limits."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class ExecutionBudget:
    max_requests: int = 6
    timeout_seconds: float = 180
    max_reported_tokens: int | None = None

    def __post_init__(self) -> None:
        if type(self.max_requests) is not int or self.max_requests <= 0:
            raise ValueError('max_requests must be a positive integer')
        if type(self.timeout_seconds) not in (int, float) or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be a finite positive number')
        if self.max_reported_tokens is not None and (
            type(self.max_reported_tokens) is not int or self.max_reported_tokens <= 0
        ):
            raise ValueError('max_reported_tokens must be a positive integer or None')


class ExecutionRequestLimitExceeded(RuntimeError):
    def __init__(self) -> None:
        super().__init__('Turn execution request limit exceeded')


class ExecutionTimeout(TimeoutError):
    def __init__(self) -> None:
        super().__init__('Turn execution timeout exceeded')


class ExecutionTokenLimitExceeded(RuntimeError):
    def __init__(self) -> None:
        super().__init__('Turn execution reported token limit reached')


class ExecutionUsageUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__('Turn execution token usage unavailable')
