"""Shared validation for a recipient-owned compression cursor."""

from storyloop_harness.core.contracts import AgentContextCheckpoint, AgentContextEntry, Snapshot


def validate_checkpoint(snapshot: Snapshot, entries: list[AgentContextEntry],
                        expected_version: int, checkpoint: AgentContextCheckpoint) -> None:
    scenario = snapshot.data.get('scenario', {})
    if (not isinstance(scenario, dict)
            or checkpoint.scenario_id != scenario.get('id')
            or checkpoint.scenario_version != scenario.get('version')):
        raise ValueError('context checkpoint scenario does not match game')
    if (checkpoint.through_version <= expected_version
            or checkpoint.through_version > snapshot.version
            or not checkpoint.summary.strip()
            or len(checkpoint.summary) > 1200):
        raise ValueError('context checkpoint cursor or summary is invalid')
    covered = [entry for entry in entries if entry.state_version <= checkpoint.through_version]
    if (not covered or covered[-1].state_version != checkpoint.through_version
            or tuple(entry.entry_id for entry in covered) != checkpoint.source_ids):
        raise ValueError('context checkpoint source range is invalid')
