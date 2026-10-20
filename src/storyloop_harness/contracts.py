"""Public turn input and shared result contract."""
from dataclasses import dataclass
from storyloop_harness.core.turn_result import TurnOutcome
from storyloop_harness.world.facts import FactQuery


@dataclass(frozen=True)
class TurnInput:
    game_id: str
    player_text: str
    turn_id: str
    scenario_version: str


@dataclass(frozen=True)
class FactQueryInput:
    game_id: str
    query: FactQuery
    turn_id: str
    scenario_version: str
