"""Run the complete fixed offline matrix and reject partial results."""
import asyncio

from storyloop_harness.context import ContextPolicy

from .cases import CASES, DATASET_ID
from .task import task


def run_local(*, task_fn=task):
    async def run():
        rows = []
        for case in CASES:
            for policy in ContextPolicy:
                try:
                    row = await task_fn(case=case, policy=policy)
                    if row['case_id'] != case['id'] or row['policy_id'] != policy.value:
                        raise ValueError('policy task returned a mismatched row')
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
                 row['result']['state_version_delta']) for row in group}) == 1
            for row in group:
                row['checks']['same_authoritative_result'] = consistent
                row['passed'] = row['passed'] and consistent
        return {'dataset_id': DATASET_ID, 'status': 'offline_fixture',
                'passed': complete and all(row['passed'] for row in rows),
                'rows': rows}
    return asyncio.run(run())
