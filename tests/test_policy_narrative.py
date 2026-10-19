"""Speech cases exercise real context selection without automatic quality judging."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest


def test_narrative_suite_selects_its_own_twelve_rows_and_keeps_default():
    from examples.policy_comparison.runner import run_local
    original = run_local()
    narrative = run_local(suite='narrative')
    assert original['dataset_id'] == 'harbor-context-clues-v1'
    assert len(original['rows']) == 12
    assert {row['case_id'] for row in original['rows']} == {'old-clue', 'recent-clue', 'unrelated'}
    assert narrative['dataset_id'] == 'harbor-narrative-clues-v2'
    assert narrative['coverage']['dataset_pairs'] == len(narrative['rows']) == 12
    assert {row['case_id'] for row in narrative['rows']} == {
        'dialogue-old-clue', 'dialogue-recent-clue', 'dialogue-unknown-clue'}
    assert narrative['passed'] is True
    assert all(row['passed'] and row['result']['interaction'] == 'speech'
               and row['result']['action_outcome'] == 'not_applicable'
               and row['result']['window_broken'] is False
               and row['checks']['speech_intent'] and row['checks']['dockhand_target']
               and row['checks']['protected_world'] for row in narrative['rows'])
    encoded = json.dumps(narrative, ensure_ascii=False)
    assert '守灯人阿禾' not in encoded and '东侧栈桥' not in encoded
    assert '码头工，请提醒我' not in encoded


def test_selection_rejects_cross_suite_cases_and_bad_suite_before_model():
    from examples.policy_comparison.runner import run_local

    def forbidden():
        raise AssertionError('model constructed')

    for options in ({'suite': 'narrative', 'case_ids': ('old-clue',)},
                    {'suite': 'contracts', 'case_ids': ('dialogue-old-clue',)},
                    {'suite': 'missing'}):
        with pytest.raises(ValueError):
            run_local(model_factory=forbidden, **options)


def test_narrative_case_definitions_and_fixture_use_selected_material_only():
    from examples.policy_comparison.narrative import NARRATIVE_CASES, NarrativeFixture
    assert [(case['id'], case['player_text']) for case in NARRATIVE_CASES] == [
        ('dialogue-old-clue', '码头工，请提醒我：brasskey731 那把黄铜钥匙该交给谁？'),
        ('dialogue-recent-clue', '码头工，刚提到的 sailmarker824 蓝帆船停在哪边？'),
        ('dialogue-unknown-clue', '码头工，coastweather999 今夜海上的风向已经确认了吗？'),
    ]
    assert all(case['reference_facts'] and case['review_expectation'] for case in NARRATIVE_CASES)

    async def reply(player_history, player_summary, player_text):
        request = {'player_history': player_history, 'player_summary': player_summary,
                   'player_input': player_text}
        response = await NarrativeFixture()([{'content': json.dumps(request, ensure_ascii=False)}])
        return response.metadata

    old = asyncio.run(reply([], '', NARRATIVE_CASES[0]['player_text']))
    assert old['interaction'] == 'speech' and old['participants'] == ['dockhand']
    assert '守灯人阿禾' not in old['beats'][0]['text']
    assert '线索不足' in old['beats'][1]['text']
    selected_old = asyncio.run(reply([], '黄铜钥匙 brasskey731 交给守灯人阿禾',
                                     NARRATIVE_CASES[0]['player_text']))
    assert '你记得' in selected_old['beats'][0]['text'] and '守灯人阿禾' in selected_old['beats'][0]['text']
    assert '守灯人阿禾' not in selected_old['beats'][1]['text']
    assert '线索不足' in selected_old['beats'][1]['text']
    split_old = asyncio.run(reply([{'text': '黄铜钥匙 brasskey731'},
                                   {'text': '守灯人阿禾'}], '', NARRATIVE_CASES[0]['player_text']))
    assert '守灯人阿禾' not in split_old['beats'][0]['text']
    recent = asyncio.run(reply([{'text': '蓝帆船 sailmarker824 停在东侧栈桥'}], '',
                               NARRATIVE_CASES[1]['player_text']))
    assert '你记得' in recent['beats'][0]['text'] and '东侧栈桥' in recent['beats'][0]['text']
    assert '东侧栈桥' not in recent['beats'][1]['text']
    assert '线索不足' in recent['beats'][1]['text']
    unknown = asyncio.run(reply([], '', NARRATIVE_CASES[2]['player_text']))
    assert '风向' in unknown['beats'][1]['text'] and ('未' in unknown['beats'][1]['text'] or '不' in unknown['beats'][1]['text'])


def test_narrative_engine_preserves_variable_speech_and_world(monkeypatch):
    from examples.policy_comparison.runner import run_local
    from examples.policy_comparison import task as subject
    actual_engine = subject.TurnEngine
    outcomes = []

    class CapturingEngine(actual_engine):
        async def run_turn(self, turn):
            result = await super().run_turn(turn)
            outcomes.append(result)
            return result

    class Speech:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(metadata={'beats': [{'kind': 'narration',
                'text': '模型原创：码头工给出了另一种说法。'}],
                'interaction': 'speech', 'participants': ['dockhand']})

    monkeypatch.setattr(subject, 'TurnEngine', CapturingEngine)
    report = run_local(suite='narrative', case_ids=('dialogue-old-clue',),
                       policy_ids=('history-recent-v1',), model_mode='sdk_mock', model_factory=Speech)
    row = report['rows'][0]
    assert row['passed'] is True
    assert outcomes[0].narration == '模型原创：码头工给出了另一种说法。'
    assert outcomes[0].snapshot.data['world']['shop_window']['broken'] is False
    assert outcomes[0].decision.intent == 'speech'
    assert outcomes[0].decision.target_ids == ['dockhand']
    assert outcomes[1].narration == outcomes[0].narration
    assert '模型原创' not in json.dumps(report, ensure_ascii=False)


def test_narrative_seed_reaches_selected_request_without_reference_injection():
    from examples.policy_comparison.narrative import NarrativeFixture
    from examples.policy_comparison.runner import run_local

    class Capture(NarrativeFixture):
        requests = []

        async def __call__(self, messages, **kwargs):
            content = messages[-1]['content']
            if isinstance(content, list):
                content = ''.join(part['text'] for part in content if part.get('type') == 'text')
            self.requests.append(json.loads(content))
            return await super().__call__(messages, **kwargs)

    for case_id, policy_id, fact, source_id in (
            ('dialogue-old-clue', 'history-relevant-v1', '黄铜钥匙 brasskey731 交给守灯人阿禾', 'player-0'),
            ('dialogue-old-clue', 'history-summary-v1', '黄铜钥匙 brasskey731 交给守灯人阿禾', 'player:16'),
            ('dialogue-recent-clue', 'history-recent-v1', '蓝帆船 sailmarker824 停在东侧栈桥', 'player-23')):
        report = run_local(suite='narrative', case_ids=(case_id,), policy_ids=(policy_id,),
                           model_mode='sdk_mock', model_factory=Capture)
        assert report['passed'] is True
        assert source_id in report['rows'][0]['source_ids']['selected']
        request = Capture.requests[-1]
        player_material = json.dumps((request['player_history'], request['player_summary']),
                                     ensure_ascii=False)
        assert fact in player_material
        encoded = json.dumps(request, ensure_ascii=False)
        assert '第0条玩家可见历史：' not in encoded
        assert '依据可见旧线索回答' not in encoded
        assert 'FOREIGN_GAME' not in encoded and 'UNSELECTED_PRIVATE' not in encoded
        if policy_id == 'history-summary-v1':
            assert request['player_summary'] == fact
            assert request['npc_contexts'][0]['own_summary'] == ''
            assert 'dockhand:16' not in report['rows'][0]['source_ids']['selected']


def test_narrative_checkpoint_sources_match_player_observations(tmp_path):
    from examples.policy_comparison.narrative import OLD_FACT, seed_narrative_history
    from examples.policy_comparison.task import _package
    from storyloop_harness.core.contracts import AgentContextCheckpoint
    from storyloop_harness.testing import InMemoryGameStore

    store = InMemoryGameStore()
    seed_narrative_history(store, _package(tmp_path))
    player_checkpoint = store.agent_context_checkpoint('case', 'player')
    assert player_checkpoint.summary == OLD_FACT
    assert player_checkpoint.source_ids == tuple(f'player-{index}' for index in range(16))
    assert store.agent_context_checkpoint('case', 'dockhand') == AgentContextCheckpoint()


def test_narrative_action_output_fails_contract():
    from examples.policy_comparison.runner import run_local

    class WrongAction:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(metadata={'beats': [{'kind': 'narration', 'text': 'wrong'}],
                'interaction': 'action',
                'participants': ['dockhand'], 'action_id': 'break_shop_window',
                'action': {'status': 'occurred'}})

    report = run_local(suite='narrative', case_ids=('dialogue-old-clue',),
                       policy_ids=('history-recent-v1',), model_mode='sdk_mock',
                       model_factory=WrongAction)
    row = report['rows'][0]
    assert row['passed'] is False
    assert row['checks']['speech_intent'] is False
    assert row['checks']['protected_world'] is False


@pytest.mark.parametrize('mutation', ['missing', 'nonbool', 'wrong_passed', 'missing_result',
                                      'forged_speech', 'forged_world'])
def test_narrative_row_validator_rejects_inconsistent_evidence(mutation):
    from examples.policy_comparison.runner import run_local
    baseline = run_local(suite='narrative', case_ids=('dialogue-old-clue',),
                         policy_ids=('history-recent-v1',))['rows'][0]

    async def altered(**kwargs):
        row = deepcopy(baseline)
        row['checks'].pop('same_authoritative_result', None)
        if mutation == 'missing':
            del row['checks']['speech_intent']
        elif mutation == 'nonbool':
            row['checks']['speech_intent'] = 1
        elif mutation == 'wrong_passed':
            row['checks']['speech_intent'] = False
        elif mutation == 'missing_result':
            del row['result']['interaction']
        elif mutation == 'forged_speech':
            row['result']['interaction'] = 'action'
        else:
            row['result']['world_unchanged'] = False
        return row

    report = run_local(suite='narrative', task_fn=altered,
                       case_ids=('dialogue-old-clue',), policy_ids=('history-recent-v1',))
    assert report['rows'][0]['error'] == {'type': 'ValueError'}


def test_narrative_fixture_group_allows_different_event_hashes():
    from examples.policy_comparison.runner import run_local
    report = run_local(suite='narrative', case_ids=('dialogue-old-clue',))
    assert len(report['rows']) == 4 and report['passed'] is True
    assert all(row['checks']['same_authoritative_result'] is True for row in report['rows'])
    assert len({row['result']['events_sha256'] for row in report['rows']}) > 1


def test_cli_accepts_narrative_suite(capsys):
    from examples.policy_comparison.__main__ import main
    assert main(['--suite', 'narrative', '--case', 'dialogue-old-clue',
                 '--policy', 'history-recent-v1']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['dataset_id'] == 'harbor-narrative-clues-v2'
    assert len(report['rows']) == 1


@pytest.mark.parametrize('case_id', ['dialogue-old-clue', 'dialogue-recent-clue',
                                     'dialogue-unknown-clue'])
@pytest.mark.parametrize('mode', ['interactive', 'novel'])
def test_full_answer_duplicate_cannot_pass_narrative_acceptance(case_id, mode, monkeypatch):
    from dataclasses import replace
    from examples.policy_comparison import task as subject
    from examples.policy_comparison.runner import run_local

    real_package = subject._package
    monkeypatch.setattr(subject, '_package', lambda root: replace(
        real_package(root), presentation_mode=mode))

    class Duplicate:
        async def __call__(self, messages, **kwargs):
            answer = '今夜海上的风向尚未确认，需要再查证。'
            return SimpleNamespace(metadata={
                'beats': [{'kind': 'narration', 'text': f'码头工说：{answer}'},
                          {'kind': 'dialogue', 'actor_id': 'dockhand', 'text': answer}],
                'interaction': 'speech', 'participants': ['dockhand']})

    row = run_local(suite='narrative', case_ids=(case_id,),
                    policy_ids=('history-recent-v1',), model_mode='sdk_mock',
                    model_factory=Duplicate)['rows'][0]
    assert row['passed'] is False
    assert row['acceptance']['runtime_contract'] == 'failed'
    assert row['acceptance']['structural_content'] == 'unavailable'
    assert row['acceptance']['bounded_duplicate_check'] == 'unavailable'
    assert row['acceptance']['semantic_quality'] == 'unreviewed'


def test_unsupported_valid_content_remains_unreviewed():
    from examples.policy_comparison.runner import run_local

    class Invented:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(metadata={
                'beats': [{'kind': 'narration', 'text': '码头工拿出一封不存在的秘信。'},
                          {'kind': 'dialogue', 'actor_id': 'dockhand',
                           'text': '我确认今夜会有金船驶来。'}],
                'interaction': 'speech', 'participants': ['dockhand']})

    report = run_local(suite='narrative', case_ids=('dialogue-unknown-clue',),
                       policy_ids=('history-recent-v1',), model_mode='sdk_mock',
                       model_factory=Invented)
    row = report['rows'][0]
    assert report['passed'] is True and row['passed'] is True
    assert row['acceptance']['structural_content'] == 'passed'
    assert row['acceptance']['bounded_duplicate_check'] == 'passed'
    assert row['acceptance']['semantic_quality'] == 'unreviewed'
    assert report['acceptance']['semantic_quality'] == 'unreviewed'


@pytest.mark.parametrize('forgery', ['semantic', 'duplicate', 'runtime'])
def test_runner_rejects_forged_acceptance(forgery):
    from examples.policy_comparison.runner import run_local
    baseline = run_local(suite='narrative', case_ids=('dialogue-old-clue',),
                         policy_ids=('history-recent-v1',))['rows'][0]

    async def altered(**kwargs):
        row = deepcopy(baseline)
        row['checks'].pop('same_authoritative_result', None)
        key = {'semantic': 'semantic_quality', 'duplicate': 'bounded_duplicate_check',
               'runtime': 'runtime_contract'}[forgery]
        row['acceptance'][key] = 'passed' if key == 'semantic_quality' else 'failed'
        return row

    row = run_local(suite='narrative', task_fn=altered,
                    case_ids=('dialogue-old-clue',),
                    policy_ids=('history-recent-v1',))['rows'][0]
    assert row['error'] == {'type': 'ValueError'}


def test_emitted_narrative_row_recomputes_acceptance_after_late_failure():
    from examples.policy_comparison.runner import run_local
    baseline = run_local(suite='narrative', case_ids=('dialogue-old-clue',),
                         policy_ids=('history-recent-v1',))['rows'][0]

    async def failed(*, evidence_sink, **kwargs):
        row = deepcopy(baseline)
        row['checks'].pop('same_authoritative_result', None)
        row['acceptance'] = {'runtime_contract': 'passed', 'semantic_quality': 'passed'}
        evidence_sink(row)
        raise RuntimeError('PRIVATE_FAILURE')

    report = run_local(suite='narrative', task_fn=failed,
                       case_ids=('dialogue-old-clue',),
                       policy_ids=('history-recent-v1',))
    row = report['rows'][0]
    assert report['passed'] is row['passed'] is False
    assert row['result'] == baseline['result']
    assert row['acceptance']['runtime_contract'] == 'failed'
    assert row['acceptance']['structural_content'] == 'passed'
    assert row['acceptance']['bounded_duplicate_check'] == 'passed'
    assert row['acceptance']['semantic_quality'] == 'unreviewed'
