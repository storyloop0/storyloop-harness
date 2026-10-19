"""Run the complete fixed offline matrix and reject partial results."""
import asyncio
import json
from math import isfinite
import re
from uuid import uuid4

from storyloop_harness.context import ContextPolicy

from .cases import suite_data
from .acceptance import acceptance
from .task import task
from .pricing import PriceSnapshot, cost_evidence
from .review import SAFE_METADATA, SAFE_ROW, make_packet, validate_paths, write_packet
from storyloop_harness.usage import summarize_attempt_usage

REQUIRED_CHECKS = frozenset({'scope', 'dispatch_match', 'state', 'model_calls',
                             'recent_clue', 'old_clue_expected', 'replay'})
NARRATIVE_REQUIRED_CHECKS = frozenset({'scope', 'dispatch_match', 'model_calls',
    'recent_clue', 'old_clue_expected', 'replay', 'speech_intent', 'dockhand_target',
    'protected_world'})

_USAGE_FIELDS = {'model': str, 'task': str, 'input_tokens': int,
                 'output_tokens': int, 'cached_input_tokens': int}
_ATTEMPT_FIELDS = {'invocation_id': str, 'ordinal': int, 'model': str,
                   'task': str, 'outcome': str, 'usage': _USAGE_FIELDS,
                   'usage_state': str, 'stream_complete': bool, 'error_type': str}
_ATTEMPT_SUMMARY_FIELDS = {
    'attempts': [_ATTEMPT_FIELDS], 'known_usage': [_USAGE_FIELDS],
    'reported_attempts': int, 'missing_attempts': int, 'invalid_attempts': int,
    'incomplete_stream_attempts': int, 'usage_complete': bool,
    'known_input_tokens': int, 'known_output_tokens': int,
    'known_cached_input_tokens': int,
}
_PHASES = ('preparation', 'projection', 'generation', 'turn', 'replay')
_ROW_FIELDS = {
    'provider_input_tokens': int, 'provider_output_tokens': int,
    'attempt_summary': _ATTEMPT_SUMMARY_FIELDS,
    'cost': str, 'known_cost': str, 'cost_currency': str,
    'cost_status': str, 'cost_reasons': [str], 'unpriced_models': [str],
    **{f'{phase}_seconds': (int, float) for phase in _PHASES},
    'timing_status': {phase: str for phase in _PHASES},
    'estimated_request_tokens': int, 'estimated_total_with_reserve': int,
    'replay_attempt_count': int,
    'replay': {'new_calls': int, 'new_events': int, 'version_delta': int,
               'same_state': bool, 'same_narration': bool, 'usage_count': int},
}
_METADATA_FIELDS = {
    **{key: str for key in SAFE_METADATA if key not in (
        'pricing', 'stream', 'supplied_generation_settings', 'generation_budget', 'budget')},
    'stream': bool,
    'supplied_generation_settings': {'temperature': (int, float), 'top_p': (int, float),
                                     'reasoning_effort': str, 'max_tokens': int},
    'generation_budget': {'max_requests': int, 'timeout_seconds': (int, float)},
    'budget': {'context_window_tokens': int, 'output_tokens': int,
               'safety_margin_tokens': int, 'max_material_tokens': int},
}
_RESULT_FIELDS = {'window_broken': bool, 'action_outcome': str,
                  'state_version_delta': int, 'state_sha256': str, 'events_sha256': str,
                  'interaction': str, 'target_ids': [str], 'world_unchanged': bool,
                  'world_sha256': str}
_MISSING = object()


def _project_value(value, shape):
    """Copy only known metric fields, including inside structured evidence."""
    if value is None:
        return None
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            return _MISSING
        return {key: projected for key, field_shape in shape.items()
                if key in value
                if (projected := _project_value(value[key], field_shape)) is not _MISSING}
    if isinstance(shape, list):
        if not isinstance(value, list):
            return _MISSING
        return [projected for item in value
                if (projected := _project_value(item, shape[0])) is not _MISSING]
    if type(value) not in (shape if isinstance(shape, tuple) else (shape,)):
        return _MISSING
    if type(value) is float and not isfinite(value):
        return _MISSING
    return value


def _validate_row(row, case, policy, suite='contracts'):
    if not isinstance(row, dict) or row.get('case_id') != case['id'] or row.get('policy_id') != policy.value:
        raise ValueError('policy task returned a mismatched row')
    checks = row.get('checks')
    result = row.get('result')
    required = NARRATIVE_REQUIRED_CHECKS if suite == 'narrative' else REQUIRED_CHECKS
    if (not isinstance(checks, dict) or not required.issubset(checks)
            or any(type(checks[name]) is not bool for name in required)
            or type(row.get('passed')) is not bool
            or row.get('acceptance') != acceptance(passed=row['passed'], content_verified=True)
            or row['passed'] is not all(checks[name] for name in required)
            or not isinstance(result, dict)
            or not {'window_broken', 'action_outcome', 'state_version_delta',
                    'state_sha256', 'events_sha256'}.issubset(result)
            or type(result['window_broken']) is not bool
            or not isinstance(result['action_outcome'], str)
            or type(result['state_version_delta']) is not int
            or any(not isinstance(result[name], str)
                   or re.fullmatch(r'[0-9a-f]{64}', result[name]) is None
                   for name in ('state_sha256', 'events_sha256'))):
        raise ValueError('policy task returned an incomplete or inconsistent row')
    if suite == 'narrative' and (
            not {'interaction', 'target_ids', 'world_unchanged', 'world_sha256'}.issubset(result)
            or result['interaction'] not in ('speech', 'inspect', 'action')
            or not isinstance(result['target_ids'], list)
            or any(not isinstance(actor, str) for actor in result['target_ids'])
            or type(result['world_unchanged']) is not bool
            or not isinstance(result['world_sha256'], str)
            or re.fullmatch(r'[0-9a-f]{64}', result['world_sha256']) is None
            or checks['speech_intent'] is not (result['interaction'] == 'speech')
            or checks['dockhand_target'] is not (result['target_ids'] == ['dockhand'])
            or checks['protected_world'] is not result['world_unchanged']):
        raise ValueError('policy task returned an incomplete or inconsistent narrative row')


def _failed_row(case, policy, run_id, error, pricing, emitted=None, suite='contracts'):
    """Retain usable failure evidence without accepting a task's success labels."""
    row = {'case_id': case['id'], 'policy_id': policy.value, 'run_id': run_id,
           'passed': False, 'error': {'type': type(error).__name__},
           'acceptance': acceptance(passed=False, content_verified=False),
           **cost_evidence(summarize_attempt_usage(()), pricing),
           'metadata': {'pricing': pricing.to_metadata() if pricing is not None else None}}
    if not isinstance(emitted, dict):
        return row
    for key in SAFE_ROW:
        if key in emitted and key in _ROW_FIELDS:
            projected = _project_value(emitted[key], _ROW_FIELDS[key])
            if projected is not _MISSING:
                row[key] = projected
    if isinstance(emitted.get('metadata'), dict):
        row['metadata'].update({key: projected for key in SAFE_METADATA
                                if key in emitted['metadata'] and key in _METADATA_FIELDS
                                if (projected := _project_value(
                                    emitted['metadata'][key], _METADATA_FIELDS[key])) is not _MISSING})
    checks = emitted.get('checks')
    required = NARRATIVE_REQUIRED_CHECKS if suite == 'narrative' else REQUIRED_CHECKS
    if (emitted.get('case_id') == case['id'] and emitted.get('policy_id') == policy.value
            and isinstance(checks, dict) and checks.get('replay') is False):
        row['checks'] = {'replay': False}
    if (emitted.get('case_id') == case['id'] and emitted.get('policy_id') == policy.value
            and isinstance(checks, dict) and required.issubset(checks)
            and all(type(checks[name]) is bool for name in required)):
        candidate = {**emitted, 'passed': all(checks[name] for name in required),
                     'acceptance': acceptance(passed=all(checks[name] for name in required),
                                              content_verified=True)}
        try:
            _validate_row(candidate, case, policy, suite)
        except (ValueError, TypeError, KeyError):
            pass
        else:
            row['checks'] = {key: value for key, value in checks.items() if key in required}
            row['result'] = _project_value(emitted['result'], _RESULT_FIELDS)
            for key, shape in (('clues', {'old_present': bool, 'recent_present': bool}),
                               ('source_ids', {'selected': [str], 'omitted': [str]})):
                if key in emitted:
                    projected = _project_value(emitted[key], shape)
                    if projected is not _MISSING:
                        row[key] = projected
            row['acceptance'] = acceptance(passed=False, content_verified=True)
    return row


def _selection(ids, available, label):
    available = tuple(available)
    if ids is None:
        return tuple(available)
    ids = tuple(ids)
    if not ids or len(ids) != len(set(ids)) or set(ids) - set(available):
        raise ValueError(f'invalid {label} selection')
    return tuple(item for item in available if item in ids)


def run_local(*, task_fn=task, case_ids=None, policy_ids=None,
              model_mode='fixture', model_factory=None, output_path=None, pricing=None,
              suite='contracts', review_output_path=None):
    dataset_id, cases = suite_data(suite)
    if review_output_path is not None and suite != 'narrative':
        raise ValueError('review output requires narrative suite')
    validate_paths(output_path, review_output_path)
    if pricing is not None and not isinstance(pricing, PriceSnapshot):
        raise ValueError('invalid pricing snapshot')
    if model_mode not in ('fixture', 'live', 'sdk_mock'):
        raise ValueError('invalid model mode')
    selected_cases = _selection(case_ids, (case['id'] for case in cases), 'case')
    selected_policies = _selection(policy_ids, (policy.value for policy in ContextPolicy), 'policy')
    if model_mode == 'live' and (len(selected_cases) != 1 or len(selected_policies) != 1
                                 or case_ids is None or policy_ids is None):
        raise ValueError('live mode requires one explicit case and policy')
    all_pairs = [(case['id'], policy.value) for case in cases for policy in ContextPolicy]
    expected = [pair for pair in all_pairs if pair[0] in selected_cases and pair[1] in selected_policies]
    run_id = uuid4().hex

    async def run():
        rows = []
        captured = {}
        status = 'offline_fixture' if model_mode == 'fixture' else model_mode

        def finalize(cancelled=False):
            actual = [(row['case_id'], row['policy_id']) for row in rows]
            complete = len(actual) == len(expected) and actual == expected
            coverage = {'dataset_pairs': len(all_pairs),
                        'selected_pairs': [{'case_id': a, 'policy_id': b} for a, b in expected],
                        'completed_pairs': [{'case_id': a, 'policy_id': b} for a, b in actual],
                        'excluded_pairs': [{'case_id': a, 'policy_id': b} for a, b in all_pairs if (a, b) not in expected],
                        'full_matrix_selected': len(expected) == len(all_pairs)}
            if not cancelled and not complete:
                raise ValueError('policy task omitted or duplicated a selected row')
            for case in cases:
                group = [row for row in rows if row['case_id'] == case['id'] and 'result' in row]
                selected_group = [pair for pair in expected if pair[0] == case['id']]
                evaluate = model_mode == 'fixture' and len(selected_group) == len(ContextPolicy) and len(group) == len(ContextPolicy)
                if evaluate and suite == 'narrative':
                    consistent = len({(row['result']['window_broken'],
                                       row['result']['action_outcome'],
                                       row['result']['world_sha256']) for row in group}) == 1
                elif evaluate:
                    consistent = len({(row['result']['window_broken'], row['result']['action_outcome'],
                                       row['result']['state_version_delta'], row['result']['state_sha256'],
                                       row['result']['events_sha256']) for row in group}) == 1
                else:
                    consistent = None
                for row in group:
                    row['checks']['same_authoritative_result'] = consistent
                    if evaluate:
                        row['passed'] = row['passed'] and consistent
                        row['acceptance'] = acceptance(passed=row['passed'], content_verified=True)
            report = {'dataset_id': dataset_id, 'run_id': run_id,
                      'status': 'cancelled' if cancelled else status,
                      'passed': not cancelled and complete and all(row['passed'] for row in rows),
                      'coverage': coverage, 'rows': rows}
            report['acceptance'] = {
                'passed_scope': 'experiment_execution',
                'runtime_matrix': 'passed' if report['passed'] else 'failed',
                'semantic_quality': 'unreviewed',
            }
            if review_output_path is not None:
                try:
                    packet = make_packet(dataset_id=dataset_id, run_id=run_id,
                                         coverage=coverage, cases=cases,
                                         rows=rows, captured=captured)
                    write_packet(review_output_path, packet)
                except Exception:
                    if report['passed'] and not cancelled:
                        raise ValueError('could not write review output') from None
                    report['review_export_error'] = 'could not write review output'
                    report['passed'] = False
            if output_path is not None:
                from pathlib import Path
                Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return report

        for case in cases:
            for policy in ContextPolicy:
                if (case['id'], policy.value) not in expected:
                    continue
                emitted = []
                pair = (case['id'], policy.value)
                def sink(row):
                    if emitted:
                        raise ValueError('duplicate task evidence')
                    emitted.append(row)
                def review_sink(payload):
                    if pair in captured or not isinstance(payload, dict) or not isinstance(payload.get('narration'), str):
                        raise ValueError('invalid review content')
                    captured[pair] = payload['narration']
                try:
                    kwargs = {'case': case, 'policy': policy, 'model_mode': model_mode,
                              'model_factory': model_factory, 'evidence_sink': sink,
                              'pricing': pricing}
                    if suite == 'narrative':
                        kwargs['suite'] = suite
                        if review_output_path is not None:
                            kwargs['review_sink'] = review_sink
                    row = await task_fn(**kwargs)
                    if emitted:
                        raise ValueError('task emitted a row and returned another')
                    _validate_row(row, case, policy, suite)
                    row['run_id'] = run_id
                    rows.append(row)
                except (asyncio.CancelledError, KeyboardInterrupt) as original_error:
                    if emitted:
                        rows.append(_failed_row(case, policy, run_id, original_error, pricing,
                                                emitted[0], suite))
                    try:
                        finalize(cancelled=True)
                    except Exception:
                        # The interrupted task remains the primary failure.
                        pass
                    raise
                except Exception as error:
                    if emitted:
                        rows.append(_failed_row(case, policy, run_id, error, pricing,
                                                emitted[0], suite))
                    else:
                        rows.append(_failed_row(case, policy, run_id, error, pricing))
        return finalize()
    return asyncio.run(run())
