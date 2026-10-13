"""Immutable aggregate of structured model attempt observations."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from storyloop_harness.models.attempts import ModelAttempt
from storyloop_harness.models.usage import ModelUsage


@dataclass(frozen=True)
class AttemptUsageSummary:
    attempts: tuple[ModelAttempt, ...]
    known_usage: tuple[ModelUsage, ...] = field(init=False)
    reported_attempts: int = field(init=False)
    missing_attempts: int = field(init=False)
    invalid_attempts: int = field(init=False)
    incomplete_stream_attempts: int = field(init=False)
    usage_complete: bool = field(init=False)
    known_input_tokens: int | None = field(init=False)
    known_output_tokens: int | None = field(init=False)
    known_cached_input_tokens: int | None = field(init=False)

    def __post_init__(self) -> None:
        unique: dict[tuple[str, int], ModelAttempt] = {}
        for record in self.attempts:
            if not isinstance(record, ModelAttempt):
                raise TypeError("records must contain ModelAttempt")
            identity = (record.invocation_id, record.ordinal)
            if identity in unique:
                if unique[identity] != record:
                    raise ValueError("conflicting model attempt identity")
                continue
            unique[identity] = record

        attempts = tuple(unique.values())
        grouped: dict[tuple[str, str], ModelUsage] = {}
        reported = missing = invalid = incomplete = 0
        for record in attempts:
            if not record.stream_complete:
                incomplete += 1
            if record.usage_state == "missing":
                missing += 1
            elif record.usage_state == "invalid":
                invalid += 1
            else:
                reported += 1
                usage = record.usage
                assert usage is not None  # ModelAttempt validates the state/usage pair.
                key = (usage.model, usage.task)
                prior = grouped.get(key)
                grouped[key] = usage if prior is None else ModelUsage(
                    usage.model, usage.task,
                    prior.input_tokens + usage.input_tokens,
                    prior.output_tokens + usage.output_tokens,
                    prior.cached_input_tokens + usage.cached_input_tokens,
                )

        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "known_usage", tuple(grouped.values()))
        object.__setattr__(self, "reported_attempts", reported)
        object.__setattr__(self, "missing_attempts", missing)
        object.__setattr__(self, "invalid_attempts", invalid)
        object.__setattr__(self, "incomplete_stream_attempts", incomplete)
        object.__setattr__(self, "usage_complete", bool(attempts) and all(
            record.usage_state == "reported" and record.stream_complete for record in attempts
        ))
        object.__setattr__(self, "known_input_tokens", sum(u.input_tokens for u in grouped.values()) if reported else None)
        object.__setattr__(self, "known_output_tokens", sum(u.output_tokens for u in grouped.values()) if reported else None)
        object.__setattr__(self, "known_cached_input_tokens", sum(u.cached_input_tokens for u in grouped.values()) if reported else None)

    def to_dict(self) -> dict:
        return {
            "attempts": [{
                "invocation_id": record.invocation_id,
                "ordinal": record.ordinal,
                "model": record.model,
                "task": record.task,
                "outcome": record.outcome,
                "usage": record.usage.to_dict() if record.usage is not None else None,
                "usage_state": record.usage_state,
                "stream_complete": record.stream_complete,
                "error_type": record.error_type,
            } for record in self.attempts],
            "known_usage": [usage.to_dict() for usage in self.known_usage],
            "reported_attempts": self.reported_attempts,
            "missing_attempts": self.missing_attempts,
            "invalid_attempts": self.invalid_attempts,
            "incomplete_stream_attempts": self.incomplete_stream_attempts,
            "usage_complete": self.usage_complete,
            "known_input_tokens": self.known_input_tokens,
            "known_output_tokens": self.known_output_tokens,
            "known_cached_input_tokens": self.known_cached_input_tokens,
        }


def summarize_attempt_usage(records: Iterable[ModelAttempt]) -> AttemptUsageSummary:
    """Freeze, validate and aggregate one set of logical structured attempts."""
    return AttemptUsageSummary(tuple(records))
