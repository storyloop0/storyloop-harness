"""Public limits for one structured model generation."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class GenerationBudget:
    max_requests: int = 3
    timeout_seconds: float = 90

    def __post_init__(self) -> None:
        if type(self.max_requests) is not int or self.max_requests <= 0:
            raise ValueError('max_requests must be a positive integer')
        if type(self.timeout_seconds) not in (int, float) or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be a finite positive number')


class GenerationRequestLimitExceeded(RuntimeError):
    def __init__(self) -> None:
        super().__init__('Structured generation request limit exceeded')


class GenerationTimeout(TimeoutError):
    def __init__(self) -> None:
        super().__init__('Structured generation timeout exceeded')
