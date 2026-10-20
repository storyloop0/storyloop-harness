"""Public offline fact-query example runs against durable storage."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_fact_query_cli_reports_verified_boundaries_without_bodies(tmp_path):
    output = tmp_path / 'acceptance.json'
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo / 'src') + os.pathsep + str(repo))
    process = subprocess.run(
        [sys.executable, '-m', 'examples.fact_query', '--output', str(output)],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'passed'
    assert report['cases'] == {
        'known': 'known', 'missing': 'unknown',
        'player_only': 'unknown', 'undisclosable': 'unknown',
    }
    assert report['model_calls'] == report['model_usage_records'] == 0
    assert report['first_query_committed_events'] > 0
    assert report['replay_committed_events'] == 0
    assert report['sqlite_reopened'] is True
    assert report['replay_equal'] is True
    assert report['summary_evidence_kind'] == 'unverified_summary'
    assert report['provenance_counts']['grounded_answer'] >= 1
    assert report['provenance_counts']['generated_memory'] >= 1
    assert report['provenance_counts']['generated_narrative'] >= 1
    assert report['provenance_counts']['legacy_unverified'] >= 1
    assert report['free_narrative_semantic_quality'] == 'unreviewed'
    serialized = json.dumps(report, ensure_ascii=False)
    for body in ('北风已确认', '南风已确认', '我不知道，或尚未确认。'):
        assert body not in serialized
