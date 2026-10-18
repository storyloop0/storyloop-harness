"""Run the complete fixed offline matrix and reject partial results."""
import asyncio
import json
import re
from uuid import uuid4

from storyloop_harness.context import ContextPolicy

from .cases import suite_data
from .task import task
from .pricing import PriceSnapshot, cost_evidence
from .review import make_packet, validate_paths, write_packet
from storyloop_harness.usage import summarize_attempt_usage

REQUIRED_CHECKS = frozenset({'scope', 'dispatch_match', 'state', 'model_calls',
                             'recent_clue', 'old_clue_expected', 'replay'})
NARRATIVE_REQUIRED_CHECKS = frozenset({'scope', 'dispatch_match', 'model_calls',
    'recent_clue', 'old_clue_expected', 'replay', 'speech_intent', 'dockhand_target',
    'protected_world'})


def _validate_row(row, case, policy, suite='contracts'):
    if not isinstance(row, dict) or row.get('case_id') != case['id'] or row.get('policy_id') != policy.value:
        raise ValueError('policy task returned a mismatched row')
    checks = row.get('checks')
    result = row.get('result')
    required = NARRATIVE_REQUIRED_CHECKS if suite == 'narrative' else REQUIRED_CHECKS
    if (not isinstance(checks, dict) or not required.issubset(checks)
            or any(type(checks[name]) is not bool for name in required)
            or type(row.get('passed')) is not bool
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
            report = {'dataset_id': dataset_id, 'run_id': run_id,
                      'status': 'cancelled' if cancelled else status,
                      'passed': not cancelled and complete and all(row['passed'] for row in rows),
                      'coverage': coverage, 'rows': rows}
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
                except (asyncio.CancelledError, KeyboardInterrupt):
                    if emitted:
                        for item in emitted:
                            item['run_id'] = run_id
                        rows.extend(emitted)
                    try:
                        finalize(cancelled=True)
                    except Exception:
                        # The interrupted task remains the primary failure.
                        pass
                    raise
                except Exception as error:
                    if emitted:
                        for item in emitted:
                            item['run_id'] = run_id
                        rows.extend(emitted)
                    else:
                        rows.append({'case_id': case['id'], 'policy_id': policy.value,
                                     'run_id': run_id,
                                     'passed': False, 'error': {'type': type(error).__name__},
                                     **cost_evidence(summarize_attempt_usage(()), pricing),
                                     'metadata': {'pricing': pricing.to_metadata() if pricing is not None else None}})
        return finalize()
    return asyncio.run(run())
