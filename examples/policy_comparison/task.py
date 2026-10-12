"""One isolated SQLite save and real TurnEngine run per policy/case pair."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter

from examples.evaluation.task import CaptureModel, Fixture
from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.agents.scene_prompt import scene_system_prompt
from storyloop_harness.agents.scene_schema import scene_output_schema
from storyloop_harness.context import ContextBudget, ContextPolicy
from storyloop_harness.core.contracts import AgentContextCheckpoint, Observation, WorldEvent
from storyloop_harness.storage import SQLiteGameStore

from .cases import DATASET_ID

SOURCE = Path(__file__).resolve().parents[1] / 'freeform'
REPO = Path(__file__).resolve().parents[2]


def _package(directory):
    manifest = json.loads((SOURCE / 'manifest.json').read_text(encoding='utf-8'))
    manifest['initial_work'] = []
    manifest['actors'].append({'id': 'unselected', 'name': 'Other worker', 'card': 'other_card'})
    manifest['initial_state']['actors']['unselected'] = {'location': 'harbor_square'}
    book = json.loads((SOURCE / 'worldbook.json').read_text(encoding='utf-8'))
    book['entries'].append({'id': 'other_card', 'kind': 'card', 'visibility': 'actor',
                            'allowed_actors': ['unselected'], 'text': 'Other worker private role.'})
    for filename, content in (('manifest.json', manifest), ('worldbook.json', book)):
        (directory / filename).write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
    return ScenarioPackage.load(directory)


def _seed(store, package):
    package.seed_game(store, 'case')
    package.seed_game(store, 'foreign')
    foreign = WorldEvent('foreign-event', 'synthetic', None, None, 0, ())
    store.commit('foreign', store.load('foreign').version, foreign,
                 (Observation('foreign-player', foreign.event_id, 'player', 'scene', 'FOREIGN_GAME', 0),), ())
    for index in range(24):
        event = WorldEvent(f'event-{index}', 'synthetic', None, None, index, ())
        text = ('brasskey731 old clue' if index == 0 else
                'sailmarker824 recent clue' if index == 23 else f'ordinary account {index}')
        observations = (
            Observation(f'player-{index}', event.event_id, 'player', 'scene', text, index),
            Observation(f'dockhand-{index}', event.event_id, 'dockhand', 'private_message',
                        f'SELECTED_PRIVATE account {index}', index),
            Observation(f'unselected-{index}', event.event_id, 'unselected', 'private_message',
                        'UNSELECTED_PRIVATE', index),
        )
        store.commit('case', store.load('case').version, event, observations, ())
    for owner in ('player', 'dockhand'):
        source_ids = tuple(f'{owner}-{index}' for index in range(16))
        store.save_agent_context_checkpoint('case', owner, -1,
            AgentContextCheckpoint(16, 'brasskey731 summarized old clue',
                                   package.package_id, package.version, source_ids))


def _hash(content):
    return sha256(content.encode('utf-8')).hexdigest()


def _revision():
    try:
        result = subprocess.run(['git', '-c', f'safe.directory={REPO.as_posix()}', 'rev-parse', 'HEAD'],
                                cwd=REPO, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


async def task(*, case, policy: ContextPolicy):
    with TemporaryDirectory(prefix='storyloop-policy-') as temp:
        root = Path(temp)
        package = _package(root)
        budget = ContextBudget()
        model = CaptureModel(Fixture())
        with SQLiteGameStore(root / 'case.sqlite3') as store:
            _seed(store, package)
            turn = TurnInput('case', case['player_text'], 'policy-turn', package.version)
            engine = TurnEngine(store, package, model, max_responders=1,
                                context_budget=budget, context_policy=policy)
            before = store.load('case')
            start = perf_counter()
            projection = engine.project_context(turn)
            projection_seconds = perf_counter() - start
            start = perf_counter()
            outcome = await engine.run_turn(turn)
            turn_seconds = perf_counter() - start
            request = deepcopy(model.request)
            decisions = projection.report.decisions
            selected = sorted({d.source_id for d in decisions if d.selected})
            omitted = sorted({d.source_id for d in decisions if not d.selected})
            player_context = {'history': request['player_history'], 'summary': request['player_summary']}
            context_text = json.dumps(player_context, ensure_ascii=False)
            recent_clue = 'sailmarker824' in context_text
            old_clue = 'brasskey731' in context_text
            expected_old = (policy is ContextPolicy.SUMMARY or policy is ContextPolicy.DEFAULT
                            or (policy is ContextPolicy.RELEVANT and case['id'] == 'old-clue'))
            encoded = json.dumps(request, ensure_ascii=False)
            player_fields = {key: value for key, value in request.items() if key != 'npc_contexts'}
            scope = ('FOREIGN_GAME' not in encoded and 'UNSELECTED_PRIVATE' not in encoded
                     and 'SELECTED_PRIVATE' not in json.dumps(player_fields)
                     and 'SELECTED_PRIVATE' in json.dumps(request['npc_contexts'])
                     and len(request['npc_contexts']) == 1
                     and request['npc_contexts'][0]['id'] == 'dockhand')
            action_outcome = next((event.details['outcome'] for event in outcome.events
                                   if 'outcome' in event.details), None)
            result = {'window_broken': outcome.snapshot.data['world']['shop_window']['broken'],
                      'action_outcome': action_outcome,
                      'state_version_delta': outcome.snapshot.version - before.version,
                      'state_sha256': _hash(json.dumps(outcome.snapshot.data, sort_keys=True,
                                                      ensure_ascii=False, separators=(',', ':'))),
                      'events_sha256': _hash(json.dumps([asdict(event) for event in outcome.events],
                                                       sort_keys=True, ensure_ascii=False,
                                                       separators=(',', ':')))}
            checks = {
                'scope': scope,
                'dispatch_match': request == projection.request,
                'state': result['window_broken'] is True and action_outcome == 'accepted',
                'model_calls': model.calls == 1,
                'recent_clue': recent_clue,
                'old_clue_expected': old_clue == expected_old,
            }
            schema_json = json.dumps(scene_output_schema(package).model_json_schema(),
                                     sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            report = projection.report
            return {
                'case_id': case['id'], 'policy_id': policy.value,
                'passed': all(checks.values()), 'checks': checks,
                'clues': {'old_present': old_clue, 'recent_present': recent_clue},
                'source_ids': {'selected': selected, 'omitted': omitted},
                'estimated_request_tokens': (report.system_tokens + report.material_tokens
                                             + report.schema_tokens + report.transport_tokens),
                'estimated_total_with_reserve': report.total_tokens,
                'projection_seconds': projection_seconds, 'turn_seconds': turn_seconds,
                'provider_input_tokens': None, 'provider_output_tokens': None, 'cost': None,
                'result': result,
                'metadata': {
                    'dataset_id': DATASET_ID, 'scenario_id': package.package_id,
                    'scenario_version': package.version, 'harness_version': version('storyloop-harness'),
                    'git_revision': _revision(), 'fixture_id': 'narrative-fixture-v1',
                    'fixture_version': '1', 'policy_id': policy.value,
                    'prompt_sha256': _hash(scene_system_prompt(package)),
                    'schema_sha256': _hash(schema_json),
                    'budget': {'context_window_tokens': budget.context_window_tokens,
                               'output_tokens': budget.output_tokens,
                               'safety_margin_tokens': budget.safety_margin_tokens,
                               'max_material_tokens': budget.max_material_tokens},
                },
            }
