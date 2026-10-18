"""Explicit local, editable human review packet for synthetic dialogue cases."""
from hashlib import sha256
import json
import os
from pathlib import Path

RUBRIC_ID = 'narrative-review-v1'
DIMENSIONS = ('factual_grounding', 'request_response', 'coherence')
RUBRIC = {
    'factual_grounding': {
        'question': 'Are factual claims supported by visible clues, or is uncertainty stated?',
        'anchors': {'0': 'Unsupported or contradicted claims',
                    '1': 'Partly supported or unclear uncertainty',
                    '2': 'Grounded claims or clear uncertainty'}},
    'request_response': {
        'question': 'Does the final speech address the player request?',
        'anchors': {'0': 'Does not address the request',
                    '1': 'Addresses only part of the request',
                    '2': 'Directly addresses the request'}},
    'coherence': {
        'question': 'Is the final speech clear and internally consistent?',
        'anchors': {'0': 'Incoherent or contradictory',
                    '1': 'Partly clear or uneven',
                    '2': 'Clear and consistent'}},
}
SAFE_METADATA = ('dataset_id', 'model_mode', 'policy_id', 'started_at',
                 'timing_version', 'pricing', 'model', 'stream', 'tool_choice_policy',
                 'structured_output_transport', 'supplied_generation_settings',
                 'generation_budget', 'harness_version', 'agentscope_version',
                 'openai_version', 'httpx_version', 'git_revision', 'case_sha256',
                 'dataset_sha256', 'fixture_id', 'fixture_version', 'scenario_id',
                 'scenario_version', 'prompt_sha256', 'schema_sha256',
                 'manifest_sha256', 'worldbook_sha256', 'budget')
SAFE_ROW = ('provider_input_tokens', 'provider_output_tokens', 'attempt_summary',
            'cost', 'known_cost', 'cost_currency', 'cost_status', 'cost_reasons',
            'unpriced_models', 'preparation_seconds', 'projection_seconds',
            'generation_seconds', 'turn_seconds', 'replay_seconds',
            'timing_status', 'estimated_request_tokens', 'estimated_total_with_reserve',
            'replay_attempt_count', 'replay')


def validate_paths(output_path, review_output_path):
    """Reject two spellings of one destination before any model construction."""
    if review_output_path is None or output_path is None:
        return
    try:
        output = Path(output_path).resolve()
        review = Path(review_output_path).resolve()
        same_file = output == review or (output.exists() and review.exists()
                                         and os.path.samefile(output, review))
    except (OSError, RuntimeError, ValueError):
        raise ValueError('invalid review output path') from None
    if same_file:
        raise ValueError('review output must differ from metrics output')


def make_packet(*, dataset_id, run_id, coverage, cases, rows, captured):
    by_id = {case['id']: case for case in cases}
    items = []
    for row in rows:
        case = by_id[row['case_id']]
        narration = captured.get((row['case_id'], row['policy_id']))
        item = {
            'case_id': row['case_id'], 'policy_id': row['policy_id'],
            'player_text': case['player_text'],
            'reference_facts': list(case['reference_facts']),
            'review_expectation': case['review_expectation'],
            'narration': narration,
            'narration_sha256': sha256(narration.encode('utf-8')).hexdigest()
                                if narration is not None else None,
            'metadata': {key: row['metadata'][key] for key in SAFE_METADATA
                         if key in row.get('metadata', {})},
            'passed': row['passed'],
            'checks': {key: value for key, value in row.get('checks', {}).items()
                       if type(value) is bool},
            'error': row.get('error'),
            'reviewer': '',
            'status': 'unreviewed' if narration is not None else 'not_available',
            'scale': {'0': 'does not meet', '1': 'partly meets', '2': 'meets'},
            'ratings': {key: {'score': None, 'evidence_note': ''} for key in DIMENSIONS},
        }
        item.update({key: row[key] for key in SAFE_ROW if key in row})
        items.append(item)
    return {'schema_version': 1, 'rubric_id': RUBRIC_ID, 'rubric': RUBRIC,
            'dataset_id': dataset_id, 'run_id': run_id, 'coverage': coverage,
            'items': items}


def write_packet(path, packet):
    try:
        Path(path).write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n',
                              encoding='utf-8')
    except Exception:
        raise ValueError('could not write review output') from None
