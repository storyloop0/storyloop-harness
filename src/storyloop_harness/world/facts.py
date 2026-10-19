"""Explicit authored answers; history and prose never supply facts to this resolver."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storyloop_harness.world.scenario import ScenarioPackage

UNKNOWN_FACT_ANSWER = '我不知道，或尚未确认。'


def validate_identifier(value: object, label: str) -> None:
    if (not isinstance(value, str) or not value
            or any(char.isspace() for char in value) or value == '*'):
        raise ValueError(f'{label} must be a nonempty explicit identifier without whitespace')


def _tick(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f'{label} must be a nonnegative integer')


@dataclass(frozen=True)
class AuthoredFact:
    id: str
    text: str
    known_by: frozenset[str]
    disclose_to: frozenset[str]
    valid_from_tick: int | None = None
    valid_until_tick: int | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.id, 'fact id')
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError('fact text must be nonempty text')
        for key in ('known_by', 'disclose_to'):
            values = getattr(self, key)
            if not isinstance(values, (set, frozenset, list, tuple)):
                raise ValueError(f'{key} must be an explicit actor collection')
            for value in values:
                validate_identifier(value, key)
            if len(set(values)) != len(values):
                raise ValueError(f'{key} has duplicate actors')
            object.__setattr__(self, key, frozenset(values))
        for key in ('valid_from_tick', 'valid_until_tick'):
            value = getattr(self, key)
            if value is not None:
                _tick(value, key)
        if (self.valid_from_tick is not None and self.valid_until_tick is not None
                and self.valid_from_tick >= self.valid_until_tick):
            raise ValueError('fact validity interval must have start before end')


@dataclass(frozen=True)
class FactQuery:
    fact_id: str
    speaker_id: str

    def __post_init__(self) -> None:
        validate_identifier(self.fact_id, 'fact_id')
        validate_identifier(self.speaker_id, 'speaker_id')


@dataclass(frozen=True)
class FactAnswer:
    known: bool
    text: str


def validate_catalog(facts: tuple[AuthoredFact, ...], actor_ids: set[str]) -> None:
    if not isinstance(facts, tuple) or any(not isinstance(f, AuthoredFact) for f in facts):
        raise ValueError('facts must be a tuple of AuthoredFact values')
    if len({fact.id for fact in facts}) != len(facts):
        raise ValueError('duplicate fact ID')
    for fact in facts:
        fact.__post_init__()
        if (fact.known_by | fact.disclose_to) - (actor_ids | {'player'}):
            raise ValueError('fact permissions contain an unknown actor')


def parse_facts(raw: object, actor_ids: set[str]) -> tuple[AuthoredFact, ...]:
    if not isinstance(raw, list):
        raise ValueError('facts must be a list')
    required = {'id', 'text', 'known_by', 'disclose_to'}
    allowed = required | {'valid_from_tick', 'valid_until_tick'}
    facts = []
    for item in raw:
        if (not isinstance(item, dict) or not required <= item.keys()
                or item.keys() - allowed
                or not isinstance(item['known_by'], list)
                or not isinstance(item['disclose_to'], list)):
            raise ValueError('invalid authored fact declaration')
        facts.append(AuthoredFact(**item))
    result = tuple(facts)
    validate_catalog(result, actor_ids)
    return result


def resolve_fact(package: ScenarioPackage, query: FactQuery, tick: int) -> FactAnswer:
    if not isinstance(query, FactQuery):
        raise ValueError('query must be a FactQuery')
    query.__post_init__()
    _tick(tick, 'query tick')
    fact = next((fact for fact in package.facts if fact.id == query.fact_id), None)
    eligible = (fact is not None and query.speaker_id in fact.known_by
                and 'player' in fact.disclose_to)
    eligible = eligible and (fact.valid_from_tick is None or tick >= fact.valid_from_tick)
    eligible = eligible and (fact.valid_until_tick is None or tick < fact.valid_until_tick)
    return FactAnswer(bool(eligible), fact.text if eligible else UNKNOWN_FACT_ANSWER)
