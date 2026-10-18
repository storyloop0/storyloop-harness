"""Frozen pricing and exact evidence for the policy comparison example."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
import json

import pytest

from examples.policy_comparison.pricing import PriceSnapshot, cost_evidence, load_pricing
from storyloop_harness.usage import ModelAttempt, ModelUsage, summarize_attempt_usage


def pricing_data(models=None):
    return {
        "schema_version": 1,
        "pricing_id": "synthetic-2026-10-18",
        "as_of": "2026-10-18",
        "currency": "USD",
        "models": models if models is not None else {
            "offline-test": {
                "input_per_million": "1",
                "output_per_million": "2",
                "cached_input_per_million": "0.5",
            }
        },
    }


def attempt(ordinal=1, model="offline-test", usage=None, state="reported",
            complete=True, outcome="failed"):
    if state == "reported" and usage is None:
        usage = ModelUsage(model, "scene_turn", 30, 3)
    return ModelAttempt("a", ordinal, model, "scene_turn", outcome, usage, state,
                        complete, None if outcome == "succeeded" else "ValueError")


def test_complete_failed_attempt_has_exact_estimated_cost():
    summary = summarize_attempt_usage([attempt()])
    result = cost_evidence(summary, PriceSnapshot.from_dict(pricing_data()))
    assert result == {
        "known_cost": "0.000036", "cost": "0.000036", "cost_currency": "USD",
        "cost_status": "estimated", "cost_reasons": [], "unpriced_models": [],
    }


def test_cached_input_is_subset_and_reported_zero_is_known_zero():
    pricing = PriceSnapshot.from_dict(pricing_data())
    summary = summarize_attempt_usage([attempt(usage=ModelUsage("offline-test", "scene_turn", 30, 3, 10))])
    assert cost_evidence(summary, pricing)["cost"] == "0.000031"
    zero = summarize_attempt_usage([attempt(usage=ModelUsage("offline-test", "scene_turn", 0, 0))])
    assert cost_evidence(zero, pricing)["cost"] == "0"
    free = pricing_data({"offline-test": dict.fromkeys(
        ("input_per_million", "output_per_million", "cached_input_per_million"), "0")})
    assert cost_evidence(summary, PriceSnapshot.from_dict(free))["cost"] == "0"


def test_multiple_models_and_duplicate_attempt_identity_are_counted_once():
    models = pricing_data()["models"] | {"another": {
        "input_per_million": "3", "output_per_million": "4",
        "cached_input_per_million": "1"}}
    first = attempt()
    second = attempt(2, "another", ModelUsage("another", "scene_turn", 2, 1, 1))
    summary = summarize_attempt_usage([first, second, first])
    assert cost_evidence(summary, PriceSnapshot.from_dict(pricing_data(models)))["cost"] == "0.000044"


def test_unknown_evidence_reasons_and_known_subtotal():
    pricing = PriceSnapshot.from_dict(pricing_data())
    priced = attempt()
    missing = attempt(2, state="missing")
    invalid = attempt(3, state="invalid")
    partial = attempt(4, complete=False)
    unpriced = attempt(5, "zeta")
    unpriced_again = attempt(6, "zeta")
    result = cost_evidence(summarize_attempt_usage(
        [priced, missing, invalid, partial, unpriced, unpriced_again]), pricing)
    assert result == {
        "known_cost": "0.000072", "cost": None, "cost_currency": "USD",
        "cost_status": "unknown", "cost_reasons": ["missing_usage", "invalid_usage",
                                               "incomplete_stream", "unpriced_model"],
        "unpriced_models": ["zeta"],
    }


@pytest.mark.parametrize("records,reason", [
    ([], "no_attempts"),
    ([attempt(state="missing")], "missing_usage"),
    ([attempt(state="invalid")], "invalid_usage"),
    ([attempt(complete=False)], "incomplete_stream"),
    ([attempt(model="unpriced")], "unpriced_model"),
])
def test_unknown_without_fully_priced_complete_evidence(records, reason):
    result = cost_evidence(summarize_attempt_usage(records), PriceSnapshot.from_dict(pricing_data()))
    assert result["cost"] is None
    assert result["cost_status"] == "unknown"
    assert result["cost_reasons"] == [reason]
    assert result["known_cost"] == ("0.000036" if reason == "incomplete_stream" else None)
    assert result["unpriced_models"] == (["unpriced"] if reason == "unpriced_model" else [])


def test_no_pricing_preserves_existing_status():
    assert cost_evidence(summarize_attempt_usage([attempt()]), None) == {
        "known_cost": None, "cost": None, "cost_currency": None,
        "cost_status": "unknown: no pricing supplied", "cost_reasons": ["no_pricing"],
        "unpriced_models": [],
    }


def test_large_token_and_long_decimal_calculation_is_exact():
    rate = "0.123456789012345678901234567890123456789"
    models = {"offline-test": {"input_per_million": rate,
                               "output_per_million": "0", "cached_input_per_million": "0"}}
    tokens = 10**40 + 1
    result = cost_evidence(summarize_attempt_usage([
        attempt(usage=ModelUsage("offline-test", "scene_turn", tokens, 0))]),
        PriceSnapshot.from_dict(pricing_data(models)))
    coefficient = int(rate.replace(".", "")) * tokens
    digits = str(coefficient).zfill(45)
    expected = (digits[:-45] + "." + digits[-45:]).rstrip("0").rstrip(".")
    assert result["cost"] == expected


@pytest.mark.parametrize("rate", ["9" * 4301, "0." + "9" * 4301],
                         ids=["long-whole", "long-fraction"])
def test_schema_valid_long_rate_has_exact_cost(rate):
    models = {"offline-test": {"input_per_million": rate,
                               "output_per_million": "0", "cached_input_per_million": "0"}}
    summary = summarize_attempt_usage([attempt(usage=ModelUsage(
        "offline-test", "scene_turn", 1_000_000, 0))])
    result = cost_evidence(summary, PriceSnapshot.from_dict(pricing_data(models)))
    assert result["cost"] == rate


def test_large_product_formats_without_integer_string_limit():
    summary = summarize_attempt_usage([attempt(usage=ModelUsage(
        "offline-test", "scene_turn", 10 ** 4307, 0))])
    result = cost_evidence(summary, PriceSnapshot.from_dict(pricing_data()))
    assert result["cost"] == "1" + "0" * 4301


def test_snapshot_is_normalized_frozen_detached_and_has_stable_hash():
    source = pricing_data()
    source["models"]["offline-test"]["input_per_million"] = "0001.0000"
    snapshot = PriceSnapshot.from_dict(source)
    source["models"]["offline-test"]["input_per_million"] = "999"
    normalized = snapshot.to_dict()
    assert normalized["models"]["offline-test"]["input_per_million"] == "1"
    normalized["models"]["offline-test"]["input_per_million"] = "777"
    metadata = snapshot.to_metadata()
    metadata["snapshot"]["models"].clear()
    assert snapshot.to_dict()["models"]["offline-test"]["input_per_million"] == "1"
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        snapshot.models["offline-test"] = None
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        snapshot.models["offline-test"].input_per_million = "999"
    equivalent = pricing_data()
    equivalent["models"]["offline-test"]["cached_input_per_million"] = "0.5000"
    assert snapshot.to_metadata()["sha256"] == PriceSnapshot.from_dict(equivalent).to_metadata()["sha256"]


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(extra=1),
    lambda d: d.pop("currency"),
    lambda d: d.update(schema_version=True),
    lambda d: d.update(schema_version=2),
    lambda d: d.update(pricing_id=""),
    lambda d: d.update(as_of="2026-02-30"),
    lambda d: d.update(as_of="2026-1-01"),
    lambda d: d.update(currency="usd"),
    lambda d: d.update(currency="ÜSD"),
    lambda d: d.update(models={"": d["models"]["offline-test"]}),
    lambda d: d.update(models={"offline-test": {"input_per_million": "1"}}),
    lambda d: d["models"]["offline-test"].update(extra="1"),
    lambda d: d["models"]["offline-test"].update(input_per_million=1),
    lambda d: d["models"]["offline-test"].update(input_per_million=True),
    lambda d: d["models"]["offline-test"].update(input_per_million="1e2"),
    lambda d: d["models"]["offline-test"].update(input_per_million="-1"),
    lambda d: d["models"]["offline-test"].update(input_per_million="NaN"),
    lambda d: d["models"]["offline-test"].update(input_per_million="Infinity"),
])
def test_invalid_schema_is_rejected_with_safe_fixed_message(mutation):
    data = pricing_data()
    mutation(data)
    with pytest.raises(ValueError, match="^invalid pricing snapshot$"):
        PriceSnapshot.from_dict(data)
    with pytest.raises(ValueError, match="^invalid pricing snapshot$"):
        PriceSnapshot(data)


def test_json_loader_rejects_duplicate_keys_recursively_and_bad_files(tmp_path):
    file = tmp_path / "pricing.json"
    file.write_text(json.dumps(pricing_data()).replace('"input_per_million": "1"',
        '"input_per_million": "1", "input_per_million": "2"'), encoding="utf-8")
    with pytest.raises(ValueError, match="^invalid pricing file$"):
        load_pricing(file)
    for contents in ("{", '{"schema_version": NaN}', "\udcff"):
        file.write_text(contents, encoding="utf-8", errors="surrogatepass")
        with pytest.raises(ValueError, match="^invalid pricing file$"):
            load_pricing(file)
    with pytest.raises(ValueError, match="^invalid pricing file$"):
        load_pricing(tmp_path / "missing.json")


def test_json_loader_and_public_constructor_validate_and_copy(tmp_path):
    file = tmp_path / "pricing.json"
    file.write_text(json.dumps(pricing_data()), encoding="utf-8")
    assert load_pricing(file).to_dict() == PriceSnapshot(pricing_data()).to_dict()


@pytest.mark.parametrize("field", ["pricing_id", "model"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high", "low"])
def test_snapshot_constructors_reject_lone_surrogate(field, surrogate):
    data = pricing_data()
    if field == "pricing_id":
        data["pricing_id"] = "synthetic" + surrogate
    else:
        data["models"] = {"offline-test" + surrogate: data["models"]["offline-test"]}
    for constructor in (PriceSnapshot, PriceSnapshot.from_dict):
        with pytest.raises(ValueError, match="^invalid pricing snapshot$"):
            constructor(data)


@pytest.mark.parametrize("field", ["pricing_id", "model"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high", "low"])
def test_json_loader_rejects_escaped_lone_surrogate(tmp_path, field, surrogate):
    data = pricing_data()
    if field == "pricing_id":
        data["pricing_id"] = "synthetic" + surrogate
    else:
        data["models"] = {"offline-test" + surrogate: data["models"]["offline-test"]}
    file = tmp_path / "pricing.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="^invalid pricing file$"):
        load_pricing(file)


def test_valid_chinese_and_supplementary_text_preserves_canonical_metadata(tmp_path):
    data = pricing_data()
    data["pricing_id"] = "合成价格-🧪"
    data["models"] = {"模型-🧪": data["models"]["offline-test"]}
    file = tmp_path / "pricing.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    expected = PriceSnapshot(data).to_dict()
    canonical = json.dumps(expected, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    metadata = load_pricing(file).to_metadata()
    assert metadata == {"snapshot": expected,
                        "sha256": sha256(canonical.encode("utf-8")).hexdigest()}
    assert metadata == PriceSnapshot.from_dict(data).to_metadata()
