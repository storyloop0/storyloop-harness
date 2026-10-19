"""Each task owns a temporary SQLite database, scenario and model instance."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.context import ContextBudget
from storyloop_harness.advanced import Observation, WorldEvent
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.usage import record_model_usage


class Fixture:
    """Intentionally claims success even when the author rule must reject it."""
    async def __call__(self, messages, **kwargs):
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        record_model_usage('narrative-fixture-v2', 'scene_turn', usage)
        return SimpleNamespace(metadata={
            'beats': [{'kind': 'narration',
                       'text': 'The window breaks; an invented secret reward appears.'}],
            'interaction': 'action', 'action_id': 'break_shop_window',
            'action': {'status': 'occurred'},
        }, usage=usage)


class CaptureModel:
    def __init__(self, delegate):
        self.delegate, self.calls, self.request = delegate, 0, None

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        content = messages[-1]['content']
        if isinstance(content, list):
            content = ''.join(p['text'] for p in content if p.get('type') == 'text')
        self.request = json.loads(content)
        return await self.delegate(messages, **kwargs)


def seed_history(store, package):
    for game in ('case', 'foreign'):
        package.seed_game(store, game)
        for index in range(24 if game == 'case' else 1):
            event_id = f'history-{index}'
            text = ('ordinary old account ' * 100 + ' brasskey731 ' + ' ordinary tail ' * 100
                    if index == 0 else f'ordinary account {index}')
            observations = (
                Observation(f'player-{index}', event_id, 'player', 'scene',
                            text if game == 'case' else 'FOREIGN_GAME', index),
                Observation(f'actor-{index}', event_id, 'dockhand', 'private_message',
                            'SELECTED_PRIVATE' if game == 'case' else 'FOREIGN_GAME', index),
                Observation(f'other-{index}', event_id, 'unselected', 'private_message',
                            'UNSELECTED_PRIVATE', index),
            )
            store.commit(game, store.load(game).version,
                         WorldEvent(event_id, 'synthetic', None, None, index, ()),
                         observations, ())


async def task(*, item, live=False, **kwargs):
    started = datetime.now(timezone.utc).isoformat()
    clock = perf_counter()
    case = item['input']
    source = Path(__file__).resolve().parents[1] / 'freeform'
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    manifest['initial_state']['world']['shop_window']['broken'] = case['window_broken']
    manifest['initial_work'] = []
    manifest['actors'].append({'id': 'unselected', 'name': 'Other worker', 'card': 'other_card'})
    manifest['initial_state']['actors']['unselected'] = {'location': 'harbor_square'}
    book = json.loads((source / 'worldbook.json').read_text(encoding='utf-8'))
    book['entries'].append({'id': 'other_card', 'kind': 'card', 'visibility': 'actor',
                            'allowed_actors': ['unselected'], 'text': 'Other worker private role.'})
    with TemporaryDirectory(prefix='storyloop-case-package-') as directory:
        for filename, data in [('manifest.json', manifest), ('worldbook.json', book)]:
            (Path(directory) / filename).write_text(json.dumps(data), encoding='utf-8')
        package = ScenarioPackage.load(directory)
    budget = ContextBudget()
    if live:
        from examples.persistent_story import live_model
        delegate = live_model()
    else:
        delegate = Fixture()
    model = CaptureModel(delegate)
    try:
        with TemporaryDirectory(prefix='storyloop-evaluation-') as directory:
            db = Path(directory) / 'case.sqlite3'
            with SQLiteGameStore(db) as store:
                seed_history(store, package)
                turn = TurnInput('case', 'dockhand brasskey731 break the window',
                                 'experiment-turn', package.version)
                engine = TurnEngine(store, package, model, max_responders=1, context_budget=budget)
                projection = engine.project_context(turn)
                outcome = await engine.run_turn(turn)
                request = deepcopy(model.request)
                before_calls = model.calls
            # Reopen the durable store and construct a new engine for duplicate replay.
            with SQLiteGameStore(db) as store:
                replay = await TurnEngine(store, package, model, max_responders=1,
                                          context_budget=budget).run_turn(turn)
            return {
                'id': case['id'], 'request': request,
                'window_broken': outcome.snapshot.data['world']['shop_window']['broken'],
                'outcome': next(e.details['outcome'] for e in outcome.events if 'outcome' in e.details),
                'narration': outcome.narration,
                'replay': {'new_calls': model.calls - before_calls,
                           'new_events': len(replay.events),
                           'version_delta': replay.snapshot.version - outcome.snapshot.version,
                           'same_state': replay.snapshot == outcome.snapshot,
                           'same_narration': replay.narration == outcome.narration,
                           'usage_count': len(replay.model_usage)},
                'metadata': {
                    'package_id': package.package_id, 'package_version': package.version,
                    'harness_version': version('storyloop-harness'),
                    'agentscope_version': version('agentscope'),
                    'model_mode': 'live' if live else 'fixture',
                    'model': getattr(delegate, 'model', 'narrative-fixture-v2'),
                    'fixture_version': None if live else '2',
                    'policy': projection.report.policy_id, 'budget': asdict(budget),
                    'projection': projection.report.to_dict(),
                    'usage': [u.to_dict() for u in outcome.model_usage],
                    'model_calls': before_calls, 'started_at': started,
                    'elapsed_seconds': perf_counter() - clock,
                    'cost': None, 'cost_status': 'unknown: no pricing supplied',
                },
            }
    finally:
        if live:
            await delegate.client.close()
