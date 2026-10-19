"""Advance recipient-owned experience summaries without deleting source events."""

from dataclasses import dataclass
from typing import Awaitable, Callable

from storyloop_harness.core.contracts import AgentContextCheckpoint, AgentContextEntry, ContextEvidence
from storyloop_harness.core.store_port import GameStore
from storyloop_harness.world.scenario import ScenarioPackage


@dataclass(frozen=True)
class CheckpointRequest:
    game_id: str
    actor_id: str
    scenario_id: str
    scenario_version: str
    previous_summary: str
    entries: tuple[AgentContextEntry, ...]
    previous_summary_evidence: ContextEvidence = ContextEvidence(kind='unverified_summary')


CheckpointSummarizer = Callable[[CheckpointRequest], Awaitable[str]]


class CheckpointLifecycle:
    """Compact old complete version groups while retaining recent raw entries."""

    def __init__(self, store: GameStore, package: ScenarioPackage,
                 summarize: CheckpointSummarizer, *, trigger_entries: int = 16,
                 retain_entries: int = 8) -> None:
        if trigger_entries < 1 or retain_entries < 1 or retain_entries >= trigger_entries:
            raise ValueError('checkpoint thresholds must leave entries to compress')
        self.store = store
        self.package = package
        self.summarize = summarize
        self.trigger_entries = trigger_entries
        self.retain_entries = retain_entries

    async def refresh(self, game_id: str, actor_id: str) -> bool:
        if actor_id not in {'player', *(actor for actor, _ in self.package.actor_cards)}:
            raise ValueError('unknown checkpoint actor')
        snapshot = self.store.load(game_id)
        scenario = snapshot.data.get('scenario', {})
        if (not isinstance(scenario, dict) or scenario.get('id') != self.package.package_id
                or scenario.get('version') != self.package.version):
            raise ValueError('saved scenario identity/version does not match checkpoint package')
        current = self.store.agent_context_checkpoint(game_id, actor_id)
        if current.summary and (current.scenario_id != self.package.package_id
                                or current.scenario_version != self.package.version):
            raise ValueError('context checkpoint scenario does not match game')
        entries = self.store.agent_context_entries(game_id, actor_id, current.through_version)
        if len(entries) < self.trigger_entries:
            return False
        first_retained_version = entries[-self.retain_entries].state_version
        covered = tuple(entry for entry in entries if entry.state_version < first_retained_version)
        if not covered:
            return False
        request = CheckpointRequest(game_id, actor_id, self.package.package_id,
                                    self.package.version, current.summary, covered,
                                    ContextEvidence('unverified_summary', current.source_ids, actor_id))
        summary = await self.summarize(request)
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 1200:
            raise ValueError('checkpoint summarizer returned an invalid summary')
        checkpoint = AgentContextCheckpoint(covered[-1].state_version, summary,
                                            self.package.package_id, self.package.version,
                                            tuple(entry.entry_id for entry in covered),
                                            ContextEvidence('unverified_summary',
                                                tuple(entry.entry_id for entry in covered), actor_id))
        self.store.save_agent_context_checkpoint(game_id, actor_id, current.through_version, checkpoint)
        return True
