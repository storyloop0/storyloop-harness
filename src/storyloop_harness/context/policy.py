"""Named, immutable ordinary-scene history projection policies."""
from dataclasses import dataclass
from enum import Enum


class ContextPolicy(str, Enum):
    DEFAULT = 'scene-projection-v1'
    RECENT = 'history-recent-v1'
    RELEVANT = 'history-relevant-v1'
    SUMMARY = 'history-summary-v1'


@dataclass(frozen=True)
class HistoryWindow:
    recent: int
    older: int
    excerpt_chars: int


@dataclass(frozen=True)
class PolicySpec:
    player: HistoryWindow
    npc: HistoryWindow
    use_checkpoint: bool


_SPECS = {
    ContextPolicy.DEFAULT: PolicySpec(HistoryWindow(10, 4, 700), HistoryWindow(8, 3, 400), True),
    ContextPolicy.RECENT: PolicySpec(HistoryWindow(10, 0, 700), HistoryWindow(8, 0, 400), False),
    ContextPolicy.RELEVANT: PolicySpec(HistoryWindow(4, 10, 700), HistoryWindow(3, 8, 400), False),
    ContextPolicy.SUMMARY: PolicySpec(HistoryWindow(6, 2, 700), HistoryWindow(5, 2, 400), True),
}


def policy_spec(policy: ContextPolicy | None) -> tuple[ContextPolicy, PolicySpec]:
    if policy is None:
        policy = ContextPolicy.DEFAULT
    if not isinstance(policy, ContextPolicy):
        raise ValueError('context policy must be a ContextPolicy member')
    return policy, _SPECS[policy]
