"""Exit a real child process after the durable scene save acknowledgement."""
import asyncio
import os
import sys
from pathlib import Path

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.core.generated_scene import GeneratedSceneRecovery
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import OfflineModel


class ExitAfterSave(SQLiteGameStore):
    def save_generated_scene_snapshot(self, snapshot):
        saved = super().save_generated_scene_snapshot(snapshot)
        print(f'child_pid={os.getpid()} snapshot_id={saved.snapshot_id}', flush=True)
        os._exit(47)


def main():
    path = Path(sys.argv[1])
    package = ScenarioPackage.load(Path(__file__).resolve().parents[1] / 'examples/freeform')
    with ExitAfterSave(path) as store:
        package.seed_game(store, 'game')
        engine = TurnEngine(store, package, OfflineModel(),
            generated_scene_recovery=GeneratedSceneRecovery(store, 'offline:v1'))
        asyncio.run(engine.run_turn(TurnInput('game', 'Hello', 'turn-1', package.version)))


if __name__ == '__main__':
    main()
