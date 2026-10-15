"""Offline child-process evidence for the optional pre-snapshot guard."""

import asyncio
import json
import os
from pathlib import Path
import sys

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.advanced import GeneratedSceneRecovery, GeneratedSceneUncertain
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.testing import OfflineModel
from storyloop_harness.usage import collect_attempts


def main():
    path, count_path = Path(sys.argv[1]), Path(sys.argv[2])
    phase, protect = sys.argv[3], sys.argv[4] == 'on'
    package = ScenarioPackage.load(Path(__file__).resolve().parents[1] / 'examples/freeform')

    class ProbeStore(SQLiteGameStore):
        def create_generated_scene_generation_marker(self, marker):
            claimed = super().create_generated_scene_generation_marker(marker)
            if phase == 'after_marker' and claimed:
                os._exit(48)
            return claimed

        def save_generated_scene_snapshot(self, snapshot):
            if phase == 'before_snapshot':
                os._exit(50)
            saved = super().save_generated_scene_snapshot(snapshot)
            if phase == 'after_snapshot':
                os._exit(51)
            return saved

    class CountedModel(OfflineModel):
        async def __call__(self, *args, **kwargs):
            with count_path.open('a', encoding='utf-8') as stream:
                stream.write('call\n')
                stream.flush()
                os.fsync(stream.fileno())
            if phase == 'during_model':
                os._exit(49)
            return await super().__call__(*args, **kwargs)

    with ProbeStore(path) as store:
        try:
            store.load('game')
        except KeyError:
            package.seed_game(store, 'game')
        engine = TurnEngine(store, package, CountedModel(), generated_scene_recovery=
            GeneratedSceneRecovery(store, 'offline:v1', protect_uncertain_generation=protect))
        turn = TurnInput('game', 'Hello', 'turn-1', package.version)
        with collect_attempts() as attempts:
            try:
                if phase == 'inspect':
                    result = engine.inspect_generated_scene_recovery(turn)
                    print(json.dumps({'inspection': result is not None,
                                      'attempts': len(attempts.records)}), flush=True)
                else:
                    result = asyncio.run(engine.run_turn(turn))
                    print(json.dumps({'usage_count': len(result.model_usage),
                                      'attempts': len(attempts.records),
                                      'events': len(result.events)}), flush=True)
            except GeneratedSceneUncertain as exc:
                print(json.dumps({'uncertain': str(exc), 'attempts': len(attempts.records)}),
                      flush=True)
                return 52
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
