"""Fixed policy selection uses the same scoped projector and budget fitter."""
import asyncio

import pytest

from scene_fixtures import source_package
from storyloop_harness import TurnEngine, TurnInput
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded, ContextPolicy
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.core.contracts import AgentContextCheckpoint, Observation, WorldEvent
from storyloop_harness.testing import InMemoryGameStore, project_scene_request


def seeded_game():
    package = source_package()
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    for index in range(24):
        event = WorldEvent(f'event-{index}', 'synthetic', None, None, index, ())
        text = ('brasskey731 old clue' if index == 0 else
                'sailmarker824 recent clue' if index == 23 else f'ordinary account {index}')
        observations = (
            Observation(f'player-{index}', event.event_id, 'player', 'scene', text, index),
            Observation(f'dockhand-{index}', event.event_id, 'dockhand', 'private_message',
                        f'dockhand account {index}', index),
            Observation(f'guard-{index}', event.event_id, 'guard', 'private_message',
                        'UNSELECTED_PRIVATE', index),
        )
        store.commit('game', store.load('game').version, event, observations, ())
    for owner in ('player', 'dockhand'):
        source_ids = tuple(f'{owner}-{index}' for index in range(16))
        store.save_agent_context_checkpoint('game', owner, -1,
            AgentContextCheckpoint(16, 'brasskey731 summarized old clue',
                                   package.package_id, package.version, source_ids))
    return store, package


def selected(result, source_id):
    return any(d.source_id == source_id and d.selected for d in result.report.decisions)


def test_named_policies_preserve_default_and_select_fixed_windows():
    store, package = seeded_game()
    snapshot = store.load('game')
    def project(policy=None):
        return SceneContextProjector(store, package, max_responders=1,
                                     context_policy=policy).project(snapshot, 'dockhand brasskey731')
    baseline = project()
    assert baseline == project(ContextPolicy.DEFAULT)
    for policy in ContextPolicy:
        result = project(policy)
        assert result.report.policy_id == policy.value
        assert selected(result, 'player-23')
        assert 'UNSELECTED_PRIVATE' not in str(result.request)
    recent = project(ContextPolicy.RECENT)
    relevant = project(ContextPolicy.RELEVANT)
    summary = project(ContextPolicy.SUMMARY)
    assert not selected(recent, 'player-0')
    assert selected(relevant, 'player-0')
    assert not selected(summary, 'player-0')
    assert selected(summary, 'player:16')
    assert 'brasskey731' in str(summary.request['player_summary'])
    assert not any(d.source_kind == 'checkpoint' for d in recent.report.decisions)
    assert not any(d.source_kind == 'checkpoint' for d in relevant.report.decisions)
    assert 'recipient-history-all-committed' in recent.report.scope
    assert 'recipient-history-after-checkpoint' in summary.report.scope


def test_invalid_policy_and_budget_failure_are_labeled():
    store, package = seeded_game()
    with pytest.raises(ValueError, match='context policy'):
        SceneContextProjector(store, package, context_policy='history-recent-v1')
    with pytest.raises(ContextBudgetExceeded) as error:
        SceneContextProjector(store, package, context_policy=ContextPolicy.RELEVANT,
                              context_budget=ContextBudget(max_material_tokens=1)).project(
                                  store.load('game'), 'dockhand brasskey731')
    assert error.value.report.policy_id == ContextPolicy.RELEVANT.value


def test_engine_and_diagnostic_helper_dispatch_identical_nondefault_request():
    async def run():
        from scene_fixtures import a_turn
        from types import SimpleNamespace
        store, package = seeded_game()
        class Capture:
            def __init__(self):
                self.messages = None
            async def __call__(self, messages, **kwargs):
                self.messages = messages
                return SimpleNamespace(metadata=a_turn())
        model = Capture()
        turn = TurnInput('game', 'dockhand brasskey731', 'policy-turn', package.version)
        engine = TurnEngine(store, package, model, max_responders=1,
                            context_policy=ContextPolicy.RELEVANT)
        projected = engine.project_context(turn)
        helper = project_scene_request(store, package, 'game', turn.player_text,
                                       max_responders=1, context_policy=ContextPolicy.RELEVANT)
        assert projected.request == helper
        await engine.run_turn(turn)
        import json
        content = model.messages[-1]['content']
        if isinstance(content, list):
            content = ''.join(part['text'] for part in content if part.get('type') == 'text')
        assert json.loads(content) == projected.request
    asyncio.run(run())
