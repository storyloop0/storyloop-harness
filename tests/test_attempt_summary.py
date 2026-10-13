"""Public summaries of structured model attempt snapshots."""

from dataclasses import FrozenInstanceError

import pytest

from storyloop_harness.usage import (
    AttemptUsageSummary,
    ModelAttempt,
    ModelUsage,
    summarize_attempt_usage,
)


def attempt(invocation="a", ordinal=1, model="m", task="scene", outcome="succeeded",
            usage=None, state="missing", complete=True, error=None):
    if outcome != "succeeded" and error is None:
        error = "ValidationError"
    return ModelAttempt(invocation, ordinal, model, task, outcome, usage, state, complete, error)


def test_reported_failures_and_successes_are_counted_once():
    bad = attempt(outcome="failed", usage=ModelUsage("m", "scene", 1000, 500), state="reported")
    good = attempt(ordinal=2, usage=ModelUsage("m", "scene", 1200, 600), state="reported")
    summary = summarize_attempt_usage([bad, good, bad])

    assert summary.attempts == (bad, good)
    assert summary.known_usage == (ModelUsage("m", "scene", 2200, 1100),)
    assert (summary.reported_attempts, summary.missing_attempts, summary.invalid_attempts,
            summary.incomplete_stream_attempts) == (2, 0, 0, 0)
    assert summary.usage_complete
    assert (summary.known_input_tokens, summary.known_output_tokens,
            summary.known_cached_input_tokens) == (2200, 1100, 0)


def test_empty_and_unknown_only_have_no_known_totals_or_complete_coverage():
    for records, expected_counts in (([], (0, 0, 0, 0)),
                                     ([attempt(), attempt(ordinal=2, state="invalid")], (0, 1, 1, 0))):
        summary = summarize_attempt_usage(records)
        assert (summary.reported_attempts, summary.missing_attempts,
                summary.invalid_attempts, summary.incomplete_stream_attempts) == expected_counts
        assert summary.known_usage == ()
        assert (summary.known_input_tokens, summary.known_output_tokens,
                summary.known_cached_input_tokens) == (None, None, None)
        assert not summary.usage_complete


def test_mixed_groups_cached_tokens_and_partial_cancelled_stream():
    records = [
        attempt(usage=ModelUsage("m", "scene", 10, 3, 2), state="reported"),
        attempt(ordinal=2, model="n", usage=ModelUsage("n", "scene", 4, 5, 1), state="reported"),
        attempt(ordinal=3, outcome="cancelled", usage=ModelUsage("m", "scene", 7, 1, 3),
                state="reported", complete=False, error="CancelledError"),
        attempt(ordinal=4, task="other", usage=ModelUsage("m", "other", 0, 0, 0), state="reported"),
    ]
    summary = summarize_attempt_usage(records)
    assert summary.known_usage == (ModelUsage("m", "scene", 17, 4, 5),
                                   ModelUsage("n", "scene", 4, 5, 1),
                                   ModelUsage("m", "other", 0, 0, 0))
    assert (summary.known_input_tokens, summary.known_output_tokens,
            summary.known_cached_input_tokens) == (21, 9, 6)
    assert summary.incomplete_stream_attempts == 1
    assert summary.reported_attempts == 4
    assert not summary.usage_complete


def test_reported_zero_is_known_zero_and_complete():
    summary = summarize_attempt_usage([attempt(usage=ModelUsage("m", "scene", 0, 0), state="reported")])
    assert summary.known_usage == (ModelUsage("m", "scene", 0, 0),)
    assert summary.known_input_tokens == summary.known_output_tokens == summary.known_cached_input_tokens == 0
    assert summary.usage_complete


def test_constructor_canonicalizes_input_and_rejects_conflicting_identity():
    first = attempt()
    records = [first, first]
    summary = AttemptUsageSummary(records)
    records.clear()
    assert summary.attempts == (first,)
    with pytest.raises(FrozenInstanceError):
        summary.attempts = ()
    for changed in (attempt(outcome="failed"),
                    attempt(usage=ModelUsage("m", "scene", 1, 0), state="reported")):
        with pytest.raises(ValueError, match="^conflicting model attempt identity$"):
            summarize_attempt_usage([first, changed])


def test_wrong_record_type_raises_fixed_error():
    for records in ([attempt(), None], [True], [ModelUsage("m", "scene", 1, 0)]):
        with pytest.raises(TypeError, match="^records must contain ModelAttempt$"):
            summarize_attempt_usage(records)


def test_to_dict_contains_all_fields_and_is_detached():
    record = attempt(outcome="failed", usage=ModelUsage("m", "scene", 2, 1, 1), state="reported")
    summary = summarize_attempt_usage([record])
    data = summary.to_dict()
    assert set(data) == {"attempts", "known_usage", "reported_attempts", "missing_attempts",
                         "invalid_attempts", "incomplete_stream_attempts", "usage_complete",
                         "known_input_tokens", "known_output_tokens", "known_cached_input_tokens"}
    assert data["attempts"] == [{"invocation_id": "a", "ordinal": 1, "model": "m", "task": "scene",
                                 "outcome": "failed", "usage": record.usage.to_dict(),
                                 "usage_state": "reported", "stream_complete": True,
                                 "error_type": "ValidationError"}]
    assert data["known_usage"] == [record.usage.to_dict()]
    data["attempts"][0]["usage"]["input_tokens"] = 999
    data["known_usage"].clear()
    assert summary.to_dict()["attempts"][0]["usage"]["input_tokens"] == 2
    assert summary.to_dict()["known_usage"] == [record.usage.to_dict()]
