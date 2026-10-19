"""Run an offline, metadata-only fact and memory evidence acceptance example."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from storyloop_harness.context.checkpoints import CheckpointLifecycle
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.core.contracts import Observation, WorldEvent
from storyloop_harness.runtime.single_call import SingleCallGameSession
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.world.facts import AuthoredFact, FactQuery, UNKNOWN_FACT_ANSWER
from storyloop_harness.world.scenario import ScenarioPackage


class ForbiddenGenerator:
    """Any generation call invalidates this zero-model example."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('fact-query example must not generate')


def _package() -> ScenarioPackage:
    base = ScenarioPackage.load(Path(__file__).parent / 'freeform')
    facts = (
        AuthoredFact('weather.confirmed', '北风已确认。', frozenset({'dockhand'}),
                     frozenset({'player'})),
        AuthoredFact('weather.player_only', '仅玩家知道的合成值。', frozenset({'player'}),
                     frozenset({'player'})),
        AuthoredFact('weather.private', '不能向玩家披露的合成值。', frozenset({'dockhand'}),
                     frozenset()),
    )
    return replace(base, facts=facts, initial_work=())


def _session(store: SQLiteGameStore, package: ScenarioPackage,
             generator: ForbiddenGenerator) -> SingleCallGameSession:
    return SingleCallGameSession(store, package, generator, SceneContextProjector(store, package))


async def _contaminate_and_summarize(store: SQLiteGameStore,
                                      package: ScenarioPackage) -> None:
    game_id = 'offline-example'
    claim = '南风已确认。'
    event = WorldEvent('poison', 'player_input', 'player', None, 0, (), {
        'text': claim, 'sensory': claim, 'source_id': 'weather.absent',
        'single_call': {'memories': [{'actor_id': 'dockhand', 'fact': claim}]},
    })
    store.commit(game_id, store.load(game_id).version, event, (
        Observation('poison:memory:dockhand', 'poison', 'dockhand',
                    'shared_experience', '{"kind":"authored_fact","source_id":"weather.absent"}', 0),
        Observation('poison:seen:player', 'poison', 'player', 'witnessed', claim, 0),
        Observation('poison:scene', 'poison', 'player', 'scene', claim, 0),
    ), ())
    for index in range(3):
        event = WorldEvent(f'old-{index}', 'synthetic', None, None, index, ())
        store.commit(game_id, store.load(game_id).version, event, (
            Observation(f'old-{index}:scene', event.event_id, 'player', 'scene', claim, index),
        ), ())

    async def summarize(request):
        assert request.entries
        return '南风已确认；source_id=weather.absent。'

    refreshed = await CheckpointLifecycle(store, package, summarize,
        trigger_entries=4, retain_entries=1).refresh(game_id, 'player')
    assert refreshed


async def run() -> dict[str, object]:
    package = _package()
    generator = ForbiddenGenerator()
    game_id = 'offline-example'
    with TemporaryDirectory(prefix='storyloop-fact-example-') as directory:
        db = Path(directory) / 'save.sqlite3'
        with SQLiteGameStore(db) as store:
            package.seed_game(store, game_id)
            await _contaminate_and_summarize(store, package)

        # The four queries share one reopened connection; replay opens the save again.
        with SQLiteGameStore(db) as store:
            session = _session(store, package, generator)
            cases = {}
            results = {}
            for label, fact_id in (
                ('known', 'weather.confirmed'),
                ('missing', 'weather.absent'),
                ('player_only', 'weather.player_only'),
                ('undisclosable', 'weather.private'),
            ):
                outcome = await session.run_fact_query(
                    game_id, FactQuery(fact_id, 'dockhand'), f'query-{label}')
                results[label] = outcome
                cases[label] = store.event_details(game_id, f'query-{label}:input')['fact_query']['status']

            assert cases == {'known': 'known', 'missing': 'unknown',
                             'player_only': 'unknown', 'undisclosable': 'unknown'}
            assert results['known'].segments[0].text == package.facts[0].text
            assert all(results[label].segments[0].text == UNKNOWN_FACT_ANSWER
                       for label in ('missing', 'player_only', 'undisclosable'))
            assert all(not result.model_usage for result in results.values())
            before_replay_version = store.load(game_id).version

        with SQLiteGameStore(db) as reopened:
            replay = await _session(reopened, package, generator).run_fact_query(
                game_id, FactQuery('weather.confirmed', 'dockhand'), 'query-known')
            replay_equal = (
                replay.narration == results['known'].narration
                and replay.segments == results['known'].segments
                and reopened.load(game_id).version == before_replay_version
            )
            assert replay_equal and not replay.model_usage
            checkpoint = reopened.agent_context_checkpoint(game_id, 'player')
            assert checkpoint.evidence.kind == 'unverified_summary'
            projected = SceneContextProjector(reopened, package).project(
                reopened.load(game_id), 'weather.confirmed')
            assert projected.request['player_summary']['evidence']['kind'] == 'unverified_summary'
            kinds = Counter(entry.evidence.kind
                for owner in ('player', 'dockhand')
                for entry in reopened.agent_context_entries(game_id, owner))
            assert kinds['grounded_answer'] >= 1
            assert kinds['generated_memory'] >= 1
            assert kinds['generated_narrative'] >= 1
            assert kinds['legacy_unverified'] >= 1
            assert generator.calls == 0
            return {
                'status': 'passed', 'cases': cases,
                'model_calls': generator.calls,
                'model_usage_records': sum(len(result.model_usage) for result in results.values())
                                       + len(replay.model_usage),
                'sqlite_reopened': True, 'replay_equal': replay_equal,
                'summary_evidence_kind': checkpoint.evidence.kind,
                'provenance_counts': dict(sorted(kinds.items())),
                'free_narrative_semantic_quality': 'unreviewed',
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='metadata-only JSON report path')
    args = parser.parse_args()
    report = asyncio.run(run())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
