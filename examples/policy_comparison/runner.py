"""Run the complete fixed offline matrix and reject partial results."""
import asyncio
import json
import re

from storyloop_harness.context import ContextPolicy

from .cases import CASES, DATASET_ID
from .task import task

REQUIRED_CHECKS = frozenset({'scope', 'dispatch_match', 'state', 'model_calls',
                             'recent_clue', 'old_clue_expected', 'replay'})


def _validate_row(row, case, policy):
    if not isinstance(row, dict) or row.get('case_id') != case['id'] or row.get('policy_id') != policy.value:
        raise ValueError('policy task returned a mismatched row')
    checks = row.get('checks')
    result = row.get('result')
    if (not isinstance(checks, dict) or not REQUIRED_CHECKS.issubset(checks)
            or any(type(checks[name]) is not bool for name in REQUIRED_CHECKS)
            or type(row.get('passed')) is not bool
            or row['passed'] is not all(checks[name] for name in REQUIRED_CHECKS)
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


def _selection(ids, available, label):
    available = tuple(available)
    if ids is None:
        return tuple(available)
    ids = tuple(ids)
    if not ids or len(ids) != len(set(ids)) or set(ids) - set(available):
        raise ValueError(f'invalid {label} selection')
    return tuple(item for item in available if item in ids)


def run_local(*, task_fn=task, case_ids=None, policy_ids=None,
              model_mode='fixture', model_factory=None, output_path=None):
    if model_mode not in ('fixture', 'live', 'sdk_mock'):
        raise ValueError('invalid model mode')
    selected_cases = _selection(case_ids, (case['id'] for case in CASES), 'case')
    selected_policies = _selection(policy_ids, (policy.value for policy in ContextPolicy), 'policy')
    if model_mode == 'live' and (len(selected_cases) != 1 or len(selected_policies) != 1
                                 or case_ids is None or policy_ids is None):
        raise ValueError('live mode requires one explicit case and policy')
    all_pairs = [(case['id'], policy.value) for case in CASES for policy in ContextPolicy]
    expected = [pair for pair in all_pairs if pair[0] in selected_cases and pair[1] in selected_policies]

    async def run():
        rows = []
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
            for case in CASES:
                group = [row for row in rows if row['case_id'] == case['id'] and 'result' in row]
                selected_group = [pair for pair in expected if pair[0] == case['id']]
                evaluate = model_mode == 'fixture' and len(selected_group) == len(ContextPolicy) and len(group) == len(ContextPolicy)
                consistent = (len({(row['result']['window_broken'], row['result']['action_outcome'],
                                    row['result']['state_version_delta'], row['result']['state_sha256'],
                                    row['result']['events_sha256']) for row in group}) == 1) if evaluate else None
                for row in group:
                    row['checks']['same_authoritative_result'] = consistent
                    if evaluate:
                        row['passed'] = row['passed'] and consistent
            report = {'dataset_id': DATASET_ID, 'status': 'cancelled' if cancelled else status,
                      'passed': not cancelled and complete and all(row['passed'] for row in rows),
                      'coverage': coverage, 'rows': rows}
            if output_path is not None:
                from pathlib import Path
                Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return report

        for case in CASES:
            for policy in ContextPolicy:
                if (case['id'], policy.value) not in expected:
                    continue
                emitted = []
                def sink(row):
                    if emitted:
                        raise ValueError('duplicate task evidence')
                    emitted.append(row)
                try:
                    row = await task_fn(case=case, policy=policy, model_mode=model_mode,
                                        model_factory=model_factory, evidence_sink=sink)
                    if emitted:
                        raise ValueError('task emitted a row and returned another')
                    _validate_row(row, case, policy)
                    rows.append(row)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    if emitted:
                        rows.extend(emitted)
                    try:
                        finalize(cancelled=True)
                    except Exception:
                        # The interrupted task remains the primary failure.
                        pass
                    raise
                except Exception as error:
                    if emitted:
                        rows.extend(emitted)
                    else:
                        rows.append({'case_id': case['id'], 'policy_id': policy.value,
                                     'passed': False, 'error': {'type': type(error).__name__}})
        return finalize()
    return asyncio.run(run())
