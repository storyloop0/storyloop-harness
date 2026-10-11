"""Play one persistent harbor turn; default model is an explicit offline fixture."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.storage import SQLiteGameStore
from storyloop_harness.usage import record_model_usage


class HarborFixture:
    """Two scripted inputs, not a natural-language model."""

    async def __call__(self, messages, *, structured_model, **kwargs):
        content = messages[-1]['content']
        if isinstance(content, list):
            content = ''.join(part['text'] for part in content if part.get('type') == 'text')
        request = json.loads(content)
        text = request['player_action']
        if text in {'打破橱窗', 'break the window'}:
            result = {'prose': '你打破了店铺橱窗。', 'interaction': 'action',
                      'action_id': 'break_shop_window', 'action': {'status': 'occurred'}}
        elif text in {'你好', 'hello'}:
            result = {'prose': '码头工向你挥手。', 'participants': ['dockhand'],
                      'replies': [{'actor_id': 'dockhand', 'speech': '今天的船已经到港。'}]}
        else:
            raise ValueError('Offline fixture accepts: 打破橱窗 / break the window / 你好 / hello')
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        record_model_usage('harbor-fixture', 'scene_turn', usage)
        return SimpleNamespace(metadata=structured_model.model_validate(result).model_dump(),
                               usage=usage)


def live_model():
    required = ('STORY_MODEL_API_KEY', 'STORY_MODEL_NAME', 'STORY_MODEL_BASE_URL')
    values = {name: os.environ.get(name, '').strip() for name in required}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError('Live mode requires: ' + ', '.join(missing))
    from agentscope.credential import OpenAICredential
    from storyloop_harness.generation import CompatibleOpenAIChatModel, ThinkingSafeOpenAIChatFormatter
    return CompatibleOpenAIChatModel(
        model=values['STORY_MODEL_NAME'],
        credential=OpenAICredential(api_key=values['STORY_MODEL_API_KEY'],
                                    base_url=values['STORY_MODEL_BASE_URL']),
        stream=False, formatter=ThinkingSafeOpenAIChatFormatter(), task='scene_turn',
    )


async def play(args):
    model = live_model() if args.live else HarborFixture()
    try:
        package = ScenarioPackage.load(Path(__file__).parent / 'freeform')
        with SQLiteGameStore(args.db) as store:
            try:
                store.load(args.game)
            except KeyError:
                package.seed_game(store, args.game)
            outcome = await TurnEngine(store, package, model).run_turn(
                TurnInput(args.game, args.text, args.turn_id, package.version))
            return {
                'game_id': args.game,
                'turn_id': args.turn_id,
                'version': outcome.snapshot.version,
                'window_broken': outcome.snapshot.data['world']['shop_window']['broken'],
                'narration': outcome.narration,
                'model_calls': len(outcome.model_usage),
                'remaining_work_ids': list(outcome.remaining_work_ids),
            }
    finally:
        if args.live:
            await model.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='SQLite file to create or resume')
    parser.add_argument('--game', default='harbor')
    parser.add_argument('--turn-id', required=True, help='Reuse only to replay the same input')
    parser.add_argument('--text', required=True)
    parser.add_argument('--live', action='store_true', help='Use your configured provider; incurs usage charges')
    args = parser.parse_args()
    try:
        result = asyncio.run(play(args))
    except ValueError as error:
        parser.exit(2, f'{error}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
