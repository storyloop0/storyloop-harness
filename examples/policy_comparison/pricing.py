"""Frozen, caller-supplied prices and exact attempt cost evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from storyloop_harness.usage import AttemptUsageSummary


_RATE_FIELDS = frozenset({"input_per_million", "output_per_million", "cached_input_per_million"})
_SNAPSHOT_FIELDS = frozenset({"schema_version", "pricing_id", "as_of", "currency", "models"})
_RATE_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}\Z")


def _invalid_snapshot() -> ValueError:
    return ValueError("invalid pricing snapshot")


def _canonical_rate(value: object) -> str:
    if not isinstance(value, str) or _RATE_PATTERN.fullmatch(value) is None:
        raise _invalid_snapshot()
    whole, _, fraction = value.partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return whole + ("." + fraction if fraction else "")


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: str
    output_per_million: str
    cached_input_per_million: str

    @classmethod
    def from_dict(cls, data: object) -> ModelPrice:
        if not isinstance(data, dict) or data.keys() != _RATE_FIELDS:
            raise _invalid_snapshot()
        return cls(*(_canonical_rate(data[field]) for field in
                     ("input_per_million", "output_per_million", "cached_input_per_million")))

    def to_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in
                ("input_per_million", "output_per_million", "cached_input_per_million")}


@dataclass(frozen=True, init=False)
class PriceSnapshot:
    schema_version: int
    pricing_id: str
    as_of: str
    currency: str
    models: Mapping[str, ModelPrice]

    def __init__(self, data: object) -> None:
        if not isinstance(data, dict) or data.keys() != _SNAPSHOT_FIELDS:
            raise _invalid_snapshot()
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise _invalid_snapshot()
        pricing_id, as_of, currency, models = (data[field] for field in
                                               ("pricing_id", "as_of", "currency", "models"))
        if not isinstance(pricing_id, str) or not pricing_id.strip():
            raise _invalid_snapshot()
        if not isinstance(as_of, str) or _DATE_PATTERN.fullmatch(as_of) is None:
            raise _invalid_snapshot()
        try:
            date.fromisoformat(as_of)
        except ValueError:
            raise _invalid_snapshot() from None
        if not isinstance(currency, str) or _CURRENCY_PATTERN.fullmatch(currency) is None:
            raise _invalid_snapshot()
        if not isinstance(models, dict):
            raise _invalid_snapshot()
        frozen_models: dict[str, ModelPrice] = {}
        for model, rates in models.items():
            if not isinstance(model, str) or not model.strip():
                raise _invalid_snapshot()
            frozen_models[model] = ModelPrice.from_dict(rates)
        object.__setattr__(self, "schema_version", 1)
        object.__setattr__(self, "pricing_id", pricing_id)
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "models", MappingProxyType(frozen_models))

    @classmethod
    def from_dict(cls, data: object) -> PriceSnapshot:
        return cls(data)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "pricing_id": self.pricing_id,
            "as_of": self.as_of,
            "currency": self.currency,
            "models": {model: price.to_dict() for model, price in self.models.items()},
        }

    def to_metadata(self) -> dict:
        snapshot = self.to_dict()
        canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return {"snapshot": snapshot, "sha256": sha256(canonical.encode("utf-8")).hexdigest()}


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("invalid pricing file")
        data[key] = value
    return data


def load_pricing(path: str | Path) -> PriceSnapshot:
    """Read and validate one JSON file without exposing its path or contents in errors."""
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file, object_pairs_hook=_unique_pairs)
        return PriceSnapshot.from_dict(data)
    except (OSError, UnicodeError, TypeError, ValueError):
        raise ValueError("invalid pricing file") from None


def _rate_parts(rate: str) -> tuple[int, int]:
    whole, _, fraction = rate.partition(".")
    return int(whole + fraction), len(fraction)


def _plain_amount(numerator: int, decimal_places: int) -> str:
    digits = str(numerator).zfill(decimal_places + 1)
    if decimal_places:
        return (digits[:-decimal_places] + "." + digits[-decimal_places:]).rstrip("0").rstrip(".")
    return digits


def cost_evidence(summary: AttemptUsageSummary, pricing: PriceSnapshot | None) -> dict:
    """Estimate only priced, reported usage from the deduplicated attempt summary."""
    if pricing is None:
        return {"cost": None, "known_cost": None, "cost_currency": None,
                "cost_status": "unknown: no pricing supplied", "cost_reasons": ["no_pricing"],
                "unpriced_models": []}

    reasons = []
    if not summary.attempts:
        reasons.append("no_attempts")
    if summary.missing_attempts:
        reasons.append("missing_usage")
    if summary.invalid_attempts:
        reasons.append("invalid_usage")
    if summary.incomplete_stream_attempts:
        reasons.append("incomplete_stream")

    unpriced = sorted({usage.model for usage in summary.known_usage
                       if usage.model not in pricing.models})
    if unpriced:
        reasons.append("unpriced_model")

    priced_usage = [(usage, pricing.models[usage.model]) for usage in summary.known_usage
                    if usage.model in pricing.models]
    known_cost = None
    if priced_usage:
        components = []
        for usage, rates in priced_usage:
            components.extend((
                (usage.input_tokens - usage.cached_input_tokens, _rate_parts(rates.input_per_million)),
                (usage.cached_input_tokens, _rate_parts(rates.cached_input_per_million)),
                (usage.output_tokens, _rate_parts(rates.output_per_million)),
            ))
        scale = max(decimal_places for _, (_, decimal_places) in components)
        numerator = sum(tokens * coefficient * 10 ** (scale - decimal_places)
                        for tokens, (coefficient, decimal_places) in components)
        known_cost = _plain_amount(numerator, scale + 6)

    complete = summary.usage_complete and not unpriced and known_cost is not None
    return {
        "cost": known_cost if complete else None,
        "known_cost": known_cost,
        "cost_currency": pricing.currency,
        "cost_status": "estimated" if complete else "unknown",
        "cost_reasons": [] if complete else reasons,
        "unpriced_models": unpriced,
    }
