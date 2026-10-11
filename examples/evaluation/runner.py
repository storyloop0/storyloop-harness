"""Two explicit execution paths for the same small set of tasks and checks."""
import asyncio
from functools import partial
from importlib.metadata import version

from .cases import CASES
from .checks import CHECK_NAMES, checks, evaluator as native_evaluator
from .task import task


def metadata(live):
    return {'model_mode': 'live' if live else 'fixture', 'case_suite': 'narrative-contracts-v1'}


def run_local(*, live=False, task_fn=task, check_fn=checks):
    async def run():
        results = []
        for case in CASES:
            try:
                output = await task_fn(item=case, live=live)
                scores = check_fn(output, case['expected_output'])
                passed = set(scores) == CHECK_NAMES and all(v is True for v in scores.values())
                results.append({'id': case['input']['id'], 'output': output,
                                'checks': scores, 'passed': passed})
            except Exception as error:
                results.append({'id': case['input']['id'], 'passed': False,
                                'error': f'{type(error).__name__}: {error}'})
        return {'passed': all(r['passed'] for r in results),
                'metadata': metadata(live), 'items': results}
    return asyncio.run(run())


def run_langfuse(client, *, live=False, task_fn=task, evaluator=native_evaluator):
    info = {**metadata(live), 'langfuse_version': version('langfuse')}
    try:
        result = client.run_experiment(name='StoryLoop narrative contracts', data=CASES,
                                       task=partial(task_fn, live=live), evaluators=[evaluator],
                                       max_concurrency=2, metadata=info)
        rows = []
        for item in result.item_results:
            scores = {score.name: score.value for score in item.evaluations}
            passed = (set(scores) == CHECK_NAMES and len(item.evaluations) == len(CHECK_NAMES)
                      and all(v == 1 for v in scores.values()))
            rows.append({'id': item.item['input']['id'], 'output': item.output,
                         'checks': scores, 'passed': passed})
        complete = sorted(r['id'] for r in rows) == sorted(c['input']['id'] for c in CASES)
        return {'passed': complete and all(r['passed'] for r in rows),
                'metadata': info, 'items': rows,
                'experiment_id': result.experiment_id, 'run_name': result.run_name,
                'dataset_run_url': result.dataset_run_url,
                'error': None if complete else 'SDK omitted one or more failed tasks'}
    except Exception as error:
        return {'passed': False, 'metadata': info, 'items': [],
                'error': f'{type(error).__name__}: {error}'}
