"""Run the complete fixed offline matrix and reject partial results."""
import asyncio

from storyloop_harness.context import ContextPolicy

from .cases import CASES, DATASET_ID
from .task import task

REQUIRED_CHECKS = frozenset({'scope', 'dispatch_match', 'state', 'model_calls',
                             'recent_clue', 'old_clue_expected'})


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
                    'state_sha256', 'events_sha256'}.issubset(result)):
        raise ValueError('policy task returned an incomplete or inconsistent row')


def run_local(*, task_fn=task):
    async def run():
        rows = []
        for case in CASES:
            for policy in ContextPolicy:
                try:
                    row = await task_fn(case=case, policy=policy)
                    _validate_row(row, case, policy)
                    rows.append(row)
                except Exception as error:
                    rows.append({'case_id': case['id'], 'policy_id': policy.value,
                                 'passed': False,
                                 'error': {'type': type(error).__name__,
                                           'message': 'Offline policy run failed'}})
        expected = {(case['id'], policy.value) for case in CASES for policy in ContextPolicy}
        actual = [(row['case_id'], row['policy_id']) for row in rows]
        complete = len(actual) == len(expected) and set(actual) == expected
        for case in CASES:
            group = [row for row in rows if row['case_id'] == case['id'] and 'result' in row]
            consistent = len(group) == len(ContextPolicy) and len({
                (row['result']['window_broken'], row['result']['action_outcome'],
                 row['result']['state_version_delta'], row['result']['state_sha256'],
                 row['result']['events_sha256'])
                for row in group}) == 1
            for row in group:
                row['checks']['same_authoritative_result'] = consistent
                row['passed'] = row['passed'] and consistent
        return {'dataset_id': DATASET_ID, 'status': 'offline_fixture',
                'passed': complete and all(row['passed'] for row in rows),
                'rows': rows}
    return asyncio.run(run())
