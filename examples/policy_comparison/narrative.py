"""Synthetic player-visible dialogue clues and an offline speech model."""
import json
from types import SimpleNamespace

from storyloop_harness.core.contracts import AgentContextCheckpoint, Observation, WorldEvent
from storyloop_harness.usage import record_model_usage

NARRATIVE_DATASET_ID = 'harbor-narrative-clues-v1'
OLD_FACT = '黄铜钥匙 brasskey731 交给守灯人阿禾'
RECENT_FACT = '蓝帆船 sailmarker824 停在东侧栈桥'
NARRATIVE_CASES = (
    {'id': 'dialogue-old-clue',
     'player_text': '码头工，请提醒我：brasskey731 那把黄铜钥匙该交给谁？',
     'reference_facts': ['第0条玩家可见历史：黄铜钥匙 brasskey731 交给守灯人阿禾。'],
     'review_expectation': '依据可见旧线索回答；缺少上下文时说明不知道，不编造收件人。'},
    {'id': 'dialogue-recent-clue',
     'player_text': '码头工，刚提到的 sailmarker824 蓝帆船停在哪边？',
     'reference_facts': ['第23条玩家可见历史：蓝帆船 sailmarker824 停在东侧栈桥。'],
     'review_expectation': '回应近期位置事实；缺少上下文时说明不知道。'},
    {'id': 'dialogue-unknown-clue',
     'player_text': '码头工，coastweather999 今夜海上的风向已经确认了吗？',
     'reference_facts': ['既有历史和公开世界资料未确认今夜海上的风向。'],
     'review_expectation': '表达信息不足或提出查证，不把预测说成已确认事实。'},
)


def seed_narrative_history(store, package):
    """Seed identical public facts and isolated private/foreign sentinels."""
    package.seed_game(store, 'case')
    package.seed_game(store, 'foreign')
    foreign = WorldEvent('foreign-event', 'synthetic', None, None, 0, ())
    store.commit('foreign', store.load('foreign').version, foreign,
                 (Observation('foreign-player', foreign.event_id, 'player', 'scene', 'FOREIGN_GAME', 0),), ())
    for index in range(24):
        event = WorldEvent(f'event-{index}', 'synthetic', None, None, index, ())
        text = (OLD_FACT if index == 0 else
                RECENT_FACT if index == 23 else
                f'港口日常见闻 {index}')
        observations = (
            Observation(f'player-{index}', event.event_id, 'player', 'scene', text, index),
            Observation(f'dockhand-{index}', event.event_id, 'dockhand', 'private_message',
                        f'SELECTED_PRIVATE account {index}', index),
            Observation(f'unselected-{index}', event.event_id, 'unselected', 'private_message',
                        'UNSELECTED_PRIVATE', index),
        )
        store.commit('case', store.load('case').version, event, observations, ())
    source_ids = tuple(f'player-{index}' for index in range(16))
    store.save_agent_context_checkpoint('case', 'player', -1,
        AgentContextCheckpoint(16, OLD_FACT,
                               package.package_id, package.version, source_ids))


class NarrativeFixture:
    """Answer only from the actual selected player history and summary."""
    async def __call__(self, messages, **kwargs):
        content = messages[-1]['content']
        if isinstance(content, list):
            content = ''.join(part['text'] for part in content if part.get('type') == 'text')
        request = json.loads(content)
        materials = [json.dumps(item, ensure_ascii=False) for item in request['player_history']]
        materials.append(json.dumps(request['player_summary'], ensure_ascii=False))
        player_action = request.get('player_action', request.get('player_input', ''))
        if 'brasskey731' in player_action and any(OLD_FACT in item for item in materials):
            speech = '那把黄铜钥匙要交给守灯人阿禾。'
        elif 'sailmarker824' in player_action and any(RECENT_FACT in item for item in materials):
            speech = '蓝帆船停在东侧栈桥。'
        elif 'coastweather999' in player_action:
            speech = '今夜海上的风向尚未确认，需要再查证。'
        else:
            speech = '现有线索不足，我不能确定答案。'
        prose = ('码头工停下手里的活，回应你的问题。'
                 if request.get('presentation_mode', 'novel') == 'interactive'
                 else f'码头工说：{speech}')
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        record_model_usage('narrative-speech-fixture-v1', 'scene_turn', usage)
        return SimpleNamespace(metadata={'prose': prose, 'interaction': 'speech',
                                         'replies': [{'actor_id': 'dockhand', 'speech': speech}],
                                         'participants': ['dockhand']}, usage=usage)
