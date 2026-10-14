"""One isolated SQLite save and real TurnEngine run per policy/case pair."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
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
from storyloop_harness.generation import GenerationBudget
from storyloop_harness.usage import collect_attempts, summarize_attempt_usage

from .cases import CASES, DATASET_ID

SOURCE = Path(__file__).resolve().parents[1] / 'freeform'
REPO = Path(__file__).resolve().parents[2]


class TimedCaptureModel(CaptureModel):
    def __init__(self, delegate):
        super().__init__(delegate)
        self.generation_seconds = None

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        content = messages[-1]['content']
        if isinstance(content, list):
            content = ''.join(part['text'] for part in content if part.get('type') == 'text')
        self.request = json.loads(content)
        started = perf_counter()
        try:
            return await self.delegate(messages, **kwargs)
        finally:
            self.generation_seconds = perf_counter() - started


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


def _supplied_settings(delegate):
    parameters = getattr(delegate, 'parameters', None)
    if parameters is None:
        return {}
    return {name: value for name in ('temperature', 'top_p', 'reasoning_effort', 'max_tokens')
            if (value := getattr(parameters, name, None)) is not None
            and type(value) in (str, int, float, bool)}


def _revision():
    try:
        result = subprocess.run(['git', '-c', f'safe.directory={REPO.as_posix()}', 'rev-parse', 'HEAD'],
                                cwd=REPO, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


async def task(*, case, policy: ContextPolicy, model_mode='fixture', model_factory=None,
               evidence_sink=None):
    started_at = datetime.now(timezone.utc).isoformat()
    empty_summary = summarize_attempt_usage(())
    evidence = {'case_id': case['id'], 'policy_id': policy.value, 'passed': False,
                'provider_input_tokens': None, 'provider_output_tokens': None,
                'attempt_summary': empty_summary.to_dict(), 'cost': None,
                'cost_status': 'unknown: no pricing supplied',
                'metadata': {'dataset_id': DATASET_ID, 'model_mode': model_mode,
                             'policy_id': policy.value, 'started_at': started_at}}
    delegate = None
    try:
        with TemporaryDirectory(prefix='storyloop-policy-') as temp:
            root = Path(temp)
            package = _package(root)
            budget = ContextBudget()
            if model_factory is not None:
                delegate = model_factory()
            elif model_mode == 'fixture':
                delegate = Fixture()
            elif model_mode == 'live':
                from examples.persistent_story import live_model
                delegate = live_model(generation_budget=GenerationBudget(3, 90))
            else:
                raise ValueError('sdk_mock requires a model_factory')
            model = TimedCaptureModel(delegate)
            generation_budget = getattr(delegate, 'generation_budget', None)
            metadata = evidence['metadata']
            metadata.update({'model': getattr(delegate, 'model', 'narrative-fixture-v1' if model_mode == 'fixture' else None),
                             'stream': getattr(delegate, 'stream', None),
                             'tool_choice_policy': getattr(delegate, 'tool_choice_policy', None),
                             'structured_output_transport': getattr(delegate, 'structured_output_transport', None),
                             'supplied_generation_settings': _supplied_settings(delegate),
                             'generation_budget': asdict(generation_budget) if generation_budget is not None else None,
                             'harness_version': version('storyloop-harness'),
                             'agentscope_version': version('agentscope'),
                             'openai_version': version('openai'), 'httpx_version': version('httpx'),
                             'git_revision': _revision(),
                             'case_sha256': _hash(json.dumps(case, sort_keys=True, ensure_ascii=False)),
                             'dataset_sha256': _hash(json.dumps(CASES, sort_keys=True, ensure_ascii=False))})
            if model_mode == 'fixture':
                metadata.update({'fixture_id': 'narrative-fixture-v1', 'fixture_version': '1'})
            evidence['metadata'] = metadata
            with SQLiteGameStore(root / 'case.sqlite3') as store:
                _seed(store, package)
                turn = TurnInput('case', case['player_text'], 'policy-turn', package.version)
                engine = TurnEngine(store, package, model, max_responders=1,
                                    context_budget=budget, context_policy=policy)
                before = store.load('case')
                start = perf_counter()
                projection = engine.project_context(turn)
                projection_seconds = perf_counter() - start
                evidence['projection_seconds'] = projection_seconds
                schema_json = json.dumps(scene_output_schema(package).model_json_schema(),
                                         sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                metadata.update({'scenario_id': package.package_id,
                                 'scenario_version': package.version,
                                 'prompt_sha256': _hash(scene_system_prompt(package)),
                                 'schema_sha256': _hash(schema_json),
                                 'manifest_sha256': _hash((root / 'manifest.json').read_text(encoding='utf-8')),
                                 'worldbook_sha256': _hash((root / 'worldbook.json').read_text(encoding='utf-8')),
                                 'budget': asdict(budget)})
                start = perf_counter()
                with collect_attempts() as attempts:
                    try:
                        outcome = await engine.run_turn(turn)
                    finally:
                        summary = summarize_attempt_usage(attempts.records)
                        evidence['attempt_summary'] = summary.to_dict()
                        evidence['provider_input_tokens'] = summary.known_input_tokens
                        evidence['provider_output_tokens'] = summary.known_output_tokens
                        evidence['turn_seconds'] = perf_counter() - start
                        evidence['generation_seconds'] = model.generation_seconds
                turn_seconds = evidence['turn_seconds']
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
                report = projection.report
                evidence.update({
                    'case_id': case['id'], 'policy_id': policy.value,
                    'passed': all(checks.values()), 'checks': checks,
                    'clues': {'old_present': old_clue, 'recent_present': recent_clue},
                    'source_ids': {'selected': selected, 'omitted': omitted},
                    'estimated_request_tokens': (report.system_tokens + report.material_tokens
                                                 + report.schema_tokens + report.transport_tokens),
                    'estimated_total_with_reserve': report.total_tokens,
                    'projection_seconds': projection_seconds, 'turn_seconds': turn_seconds,
                    'result': result,
                })
                return evidence
    except BaseException as error:
        evidence['error'] = {'type': type(error).__name__}
        evidence['passed'] = False
        if evidence_sink is not None:
            evidence_sink(evidence)
        raise
    finally:
        if delegate is not None and model_mode != 'fixture':
            client = getattr(delegate, 'client', None)
            if client is not None:
                try:
                    await client.close()
                except BaseException:
                    pass
