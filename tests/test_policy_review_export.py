"""Opt-in narrative packets expose only first player-visible outcomes."""
import asyncio
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest


def _one(**kwargs):
    from examples.policy_comparison.runner import run_local
    return run_local(suite='narrative', case_ids=('dialogue-old-clue',),
                     policy_ids=('history-recent-v1',), **kwargs)


def test_opt_in_exports_real_first_outcome_and_keeps_metrics_private(tmp_path):
    from examples.policy_comparison import task as subject
    first = '码头工说：虚构答案是西边。\n第二行 Ω'
    second = '码头工说：另一个答案。'
    outcomes = []
    real_engine = subject.TurnEngine

    class CapturingEngine(real_engine):
        async def run_turn(self, turn):
            outcome = await super().run_turn(turn)
            outcomes.append(outcome.narration)
            return outcome

    class Model:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(metadata={'prose': first, 'interaction': 'speech',
                                             'participants': ['dockhand']})

    from pytest import MonkeyPatch
    with MonkeyPatch.context() as patch:
        patch.setattr(subject, 'TurnEngine', CapturingEngine)
        metrics = tmp_path / 'metrics.json'
        review = tmp_path / 'review.json'
        report = _one(model_mode='sdk_mock', model_factory=Model,
                      output_path=metrics, review_output_path=review)
    assert outcomes == [first, first]
    assert report == json.loads(metrics.read_text(encoding='utf-8'))
    packet = json.loads(review.read_text(encoding='utf-8'))
    item = packet['items'][0]
    assert packet['schema_version'] == 1 and packet['rubric_id'] == 'narrative-review-v1'
    assert set(packet['rubric']) == {'factual_grounding', 'request_response', 'coherence'}
    assert all(set(dimension['anchors']) == {'0', '1', '2'}
               for dimension in packet['rubric'].values())
    assert packet['dataset_id'] == report['dataset_id']
    assert packet['coverage'] == report['coverage']
    assert packet['run_id'] == report['run_id'] == report['rows'][0]['run_id']
    assert item['narration'] == first and item['narration'] != second
    assert item['narration_sha256'] == sha256(first.encode('utf-8')).hexdigest()
    assert item['player_text'].startswith('码头工，请提醒我')
    assert item['reference_facts'] and item['status'] == 'unreviewed'
    assert item['reviewer'] == ''
    assert {key: value['score'] for key, value in item['ratings'].items()} == {
        'factual_grounding': None, 'request_response': None, 'coherence': None}
    assert item['cost'] is None and report['rows'][0]['cost'] is None
    encoded = metrics.read_text(encoding='utf-8')
    assert first not in encoded and '守灯人阿禾' not in encoded
    assert '码头工，请提醒我' not in encoded and 'ratings' not in encoded
    assert report['passed'] is True  # Contract success is not a narrative quality grade.


def test_default_never_exports_or_invokes_content_callback(tmp_path):
    from examples.policy_comparison.task import task
    from examples.policy_comparison.narrative import NARRATIVE_CASES
    from storyloop_harness.context import ContextPolicy

    async def run():
        return await task(case=NARRATIVE_CASES[0], policy=ContextPolicy.RECENT,
                          suite='narrative')

    assert asyncio.run(run())['passed'] is True
    report = _one(output_path=tmp_path / 'metrics.json')
    assert report['passed'] is True
    assert list(tmp_path.iterdir()) == [tmp_path / 'metrics.json']


def test_preparation_failure_has_no_review_text(tmp_path):
    def broken():
        raise RuntimeError('PRIVATE_PREPARATION')

    report = _one(model_mode='sdk_mock', model_factory=broken,
                  review_output_path=tmp_path / 'review.json')
    item = json.loads((tmp_path / 'review.json').read_text(encoding='utf-8'))['items'][0]
    assert report['passed'] is False
    assert item['status'] == 'not_available' and item['narration'] is None
    assert item['narration_sha256'] is None
    assert 'PRIVATE_PREPARATION' not in json.dumps(item, ensure_ascii=False)


def test_replay_failure_keeps_first_text_and_failed_contract(tmp_path, monkeypatch):
    from examples.policy_comparison import task as subject
    real_engine = subject.TurnEngine
    engines = []

    class Engine(real_engine):
        def __init__(self, *args, **kwargs):
            engines.append(self)
            super().__init__(*args, **kwargs)

        async def run_turn(self, turn):
            if len(engines) == 2 and self is engines[1]:
                raise RuntimeError('PRIVATE_REPLAY')
            return await super().run_turn(turn)

    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    report = _one(review_output_path=tmp_path / 'review.json')
    item = json.loads((tmp_path / 'review.json').read_text(encoding='utf-8'))['items'][0]
    assert report['passed'] is False and report['rows'][0]['checks']['replay'] is False
    assert item['narration'] and item['status'] == 'unreviewed'
    assert item['checks']['replay'] is False and item['passed'] is False
    assert 'PRIVATE_REPLAY' not in json.dumps(item, ensure_ascii=False)


def test_cancel_preserves_identity_and_partial_review(tmp_path, monkeypatch):
    from examples.policy_comparison import task as subject
    real_engine = subject.TurnEngine
    original = asyncio.CancelledError('PRIVATE_CANCEL')
    engines = []

    class Engine(real_engine):
        def __init__(self, *args, **kwargs):
            engines.append(self)
            super().__init__(*args, **kwargs)

        async def run_turn(self, turn):
            if len(engines) == 2 and self is engines[1]:
                raise original
            return await super().run_turn(turn)

    monkeypatch.setattr(subject, 'TurnEngine', Engine)
    output = tmp_path / 'metrics.json'
    review = tmp_path / 'review.json'
    with pytest.raises(asyncio.CancelledError) as caught:
        _one(output_path=output, review_output_path=review)
    assert caught.value is original
    report = json.loads(output.read_text(encoding='utf-8'))
    item = json.loads(review.read_text(encoding='utf-8'))['items'][0]
    assert report['status'] == 'cancelled' and report['passed'] is False
    assert item['narration'] and item['checks']['replay'] is False
    assert 'PRIVATE_CANCEL' not in json.dumps(report, ensure_ascii=False)


def test_attempt_summary_failure_after_outcome_keeps_review_text(tmp_path, monkeypatch):
    from examples.policy_comparison import task as subject
    summarize = subject.summarize_attempt_usage
    calls = 0

    def fail_after_first(records):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('PRIVATE_SUMMARY')
        return summarize(records)

    monkeypatch.setattr(subject, 'summarize_attempt_usage', fail_after_first)
    report = _one(review_output_path=tmp_path / 'review.json')
    item = json.loads((tmp_path / 'review.json').read_text(encoding='utf-8'))['items'][0]
    assert report['rows'][0]['passed'] is False
    assert report['rows'][0]['timing_status']['turn'] == 'completed'
    assert item['narration'] and item['narration_sha256']
    assert 'PRIVATE_SUMMARY' not in json.dumps(item, ensure_ascii=False)


def test_review_callback_error_is_safe_and_evidence_sink_once():
    from examples.policy_comparison.task import task
    from examples.policy_comparison.narrative import NARRATIVE_CASES
    from storyloop_harness.context import ContextPolicy
    emitted = []
    invoked = []

    def fail(payload):
        invoked.append(payload)
        raise RuntimeError('PRIVATE_CALLBACK')

    async def run():
        return await task(case=NARRATIVE_CASES[0], policy=ContextPolicy.RECENT,
                          suite='narrative', evidence_sink=emitted.append, review_sink=fail)

    with pytest.raises(ValueError, match='^could not capture review output$'):
        asyncio.run(run())
    assert len(invoked) == len(emitted) == 1
    assert set(invoked[0]) == {'narration'} and isinstance(invoked[0]['narration'], str)
    assert emitted[0]['error'] == {'type': 'ValueError'} and emitted[0]['passed'] is False
    assert 'PRIVATE_CALLBACK' not in json.dumps(emitted[0], ensure_ascii=False)


def test_invalid_review_selection_and_same_file_rejected_before_model(tmp_path):
    from examples.policy_comparison.runner import run_local
    def forbidden():
        raise AssertionError('model constructed')
    target = tmp_path / 'same.json'
    for options in ({'suite': 'contracts', 'review_output_path': target},
                    {'suite': 'narrative', 'output_path': target, 'review_output_path': target},
                    {'suite': 'narrative', 'output_path': target,
                     'review_output_path': tmp_path / '.' / 'same.json'}):
        with pytest.raises(ValueError):
            run_local(model_factory=forbidden, **options)


def test_invalid_review_path_error_does_not_echo_path_before_model(tmp_path):
    from examples.policy_comparison.runner import run_local
    def forbidden():
        raise AssertionError('model constructed')
    with pytest.raises(ValueError, match='^invalid review output path$'):
        run_local(suite='narrative', model_factory=forbidden,
                  output_path=tmp_path / 'metrics.json',
                  review_output_path='PRIVATE_PATH\0.json')


def test_review_writer_failure_is_safe_and_report_agrees(tmp_path, monkeypatch):
    from examples.policy_comparison import runner as subject
    def broken(*args, **kwargs):
        raise OSError('SECRET_CONTENT_AND_PATH')
    monkeypatch.setattr(subject, 'write_packet', broken)
    with pytest.raises(ValueError, match='^could not write review output$'):
        _one(review_output_path=tmp_path / 'review.json')


def test_review_writer_failure_on_failed_row_keeps_metrics_failure(tmp_path, monkeypatch):
    from examples.policy_comparison import runner as subject
    def broken(*args, **kwargs):
        raise OSError('SECRET_CONTENT_AND_PATH')
    def bad_model():
        raise RuntimeError('PRIVATE_BUSINESS')
    monkeypatch.setattr(subject, 'write_packet', broken)
    output = tmp_path / 'metrics.json'
    report = _one(model_mode='sdk_mock', model_factory=bad_model,
                  output_path=output, review_output_path=tmp_path / 'review.json')
    assert report == json.loads(output.read_text(encoding='utf-8'))
    assert report['passed'] is False
    assert report['rows'][0]['error'] == {'type': 'RuntimeError'}
    assert report['review_export_error'] == 'could not write review output'
    assert 'SECRET_CONTENT_AND_PATH' not in json.dumps(report)


def test_cli_review_flag_does_not_enable_live(tmp_path, monkeypatch, capsys):
    from examples.policy_comparison.__main__ import main
    from examples.policy_comparison import task as subject
    def forbidden(*args, **kwargs):
        raise AssertionError('live model constructed')
    monkeypatch.setattr(subject, 'GenerationBudget', forbidden)
    review = tmp_path / 'review.json'
    assert main(['--suite', 'narrative', '--case', 'dialogue-old-clue',
                 '--policy', 'history-recent-v1', '--review-output', str(review)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'offline_fixture'
    assert review.exists()
