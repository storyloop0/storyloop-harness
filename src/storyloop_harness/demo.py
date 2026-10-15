"""Installed, deterministic, credential-free Harness demonstration."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import OfflineModel


GAME_ID = 'installed-harbor-demo'


@dataclass(frozen=True)
class DemoResult:
    narration: str
    snapshot_version: int
    new_event_count: int
    fixture_model_invocations: int
    replay: bool


async def _play(text: str, turn_id: str, db_path: Path) -> DemoResult:
    with as_file(files('storyloop_harness').joinpath('demo_scenario')) as resource_dir:
        package = ScenarioPackage.load(resource_dir)
    with SQLiteGameStore(db_path) as store:
        try:
            store.load(GAME_ID)
        except KeyError:
            package.seed_game(store, GAME_ID)
        outcome = await TurnEngine(store, package, OfflineModel()).run_turn(
            TurnInput(GAME_ID, text, turn_id, package.version))
    event_count = len(outcome.events)
    invocation_count = len(outcome.model_usage)
    return DemoResult(outcome.narration, outcome.snapshot.version, event_count,
                      invocation_count, event_count == 0 and invocation_count == 0)


def _print_result(result: DemoResult, storage: str) -> None:
    print('Deterministic offline Harness demonstration')
    print(f'Storage: {storage}')
    print(result.narration)
    print(f'Snapshot version: {result.snapshot_version}')
    print(f'New events: {result.new_event_count}')
    print(f'Fixture model invocations: {result.fixture_model_invocations}')
    print('Provider requests: 0')
    print(f'Unchanged replay: {"yes" if result.replay else "no"}')


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text', default='Hello')
    parser.add_argument('--turn-id', default='demo-1')
    parser.add_argument('--db', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.db is None:
            with TemporaryDirectory(prefix='storyloop-harness-demo-') as directory:
                result = asyncio.run(_play(args.text, args.turn_id, Path(directory) / 'story.sqlite3'))
            storage = 'temporary (removed after this turn)'
        else:
            db_path = args.db.resolve()
            if not db_path.parent.is_dir():
                raise ValueError(f'database parent does not exist: {db_path.parent}')
            result = asyncio.run(_play(args.text, args.turn_id, db_path))
            storage = str(db_path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'Demo error: {exc}', file=sys.stderr)
        return 1
    _print_result(result, storage)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
