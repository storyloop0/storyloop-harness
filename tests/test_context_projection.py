"""Observable selection and metadata-only diagnostics."""
import asyncio
import json
from dataclasses import replace
import pytest
from scene_fixtures import source_package
from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.world.worldbook import Worldbook, WorldbookEntry


def fixture(package=None):
    package = package or source_package()
    store = InMemoryGameStore()
    package.seed_game(store, 'game')
    return store, package


def test_projection_is_deterministic_detached_and_reports_focus_without_content():
    store, package = fixture()
    projector = SceneContextProjector(store, package)
    first = projector.project(store.load('game'), 'Guide hello')
    second = projector.project(store.load('game'), 'Guide hello')
    assert first.report == second.report
    assert first.focus_actor_ids[0] == 'guide'
    decisions = first.report.decisions
    assert any(d.source_id == 'guide' and d.selection_reason == 'mentioned' for d in decisions)
    assert any(d.source_id == 'harbor' and d.source_kind == 'source_fact' for d in decisions)
    encoded = json.dumps(first.report.to_dict())
    assert 'Visitors arrive' not in encoded and 'knows only' not in encoded
    assert first.report.total_tokens <= first.budget.context_window_tokens
    first.request['npc_contexts'][0]['role_card'] = 'changed'
    assert second.request['npc_contexts'][0]['role_card'] != 'changed'


def test_every_public_rule_is_required_without_clipping():
    store, base = fixture()
    rules = [WorldbookEntry(f'rule-{i}', f'Rule {i} ' + 'x' * 900, 'public', frozenset(), 'rule') for i in range(7)]
    package = replace(base, worldbook=Worldbook(base.package_id, base.version, [*base.worldbook._entries.values(), *rules]))
    result = SceneContextProjector(store, package).project(store.load('game'), 'hello')
    assert result.request['public_rules'] == [r.text for r in rules]
    assert all(d.selected and not d.truncated for d in result.report.decisions if d.source_kind == 'worldbook_rule')
    with pytest.raises(ContextBudgetExceeded) as error:
        SceneContextProjector(store, package, context_budget=ContextBudget(max_material_tokens=800)).project(store.load('game'), 'hello')
    assert all(d.selected for d in error.value.report.decisions if d.source_kind == 'worldbook_rule')
    assert 'Rule 0' not in str(error.value)


def test_duplicate_facts_keep_ids_and_drop_whole_for_material_budget():
    store, base = fixture()
    facts = [{'id': f'fact_{i}', 'text': 'Repeated source material ' * 40, 'source_ref': 'private descriptive reference'} for i in range(6)]
    blueprint = base.story_blueprint.model_copy(update={'facts': [type(base.story_blueprint.facts[0]).model_validate(f) for f in facts]})
    package = replace(base, story_blueprint=blueprint)
    complete = SceneContextProjector(store, package).project(store.load('game'), 'hello')
    result = SceneContextProjector(store, package, context_budget=ContextBudget(max_material_tokens=3500)).project(store.load('game'), 'hello')
    decisions = [d for d in result.report.decisions if d.source_kind == 'source_fact']
    assert {d.source_id for d in decisions} == {f'fact_{i}' for i in range(6)}
    assert len(result.request['source_story_facts']) < len(complete.request['source_story_facts'])
    assert any(d.omission_reason == 'material_budget' for d in decisions)
    assert 'private descriptive reference' not in json.dumps(result.report.to_dict())
    assert all(item['role_card'].strip() for item in result.request['npc_contexts'])


def test_history_has_source_owner_and_reasons_before_clipping_and_exclusion():
    from storyloop_harness.core.contracts import Observation, WorldEvent
    store, package = fixture()
    for i in range(24):
        text = ('ancient treasure secret ' + 'q' * 800) if i == 0 else 'ordinary conversation ' + str(i)
        event = WorldEvent(f'event-{i}', 'synthetic', None, None, i, ())
        store.commit('game', store.load('game').version, event,
            (Observation(f'player-{i}', event.event_id, 'player', 'scene', text, i),
             Observation(f'guide-{i}', event.event_id, 'guide', 'private_message', 'own private memory ' + str(i), i),
             Observation(f'guard-{i}', event.event_id, 'guard', 'private_message', 'UNSELECTED_PRIVATE', i)), ())
    result = SceneContextProjector(store, package, max_responders=1).project(store.load('game'), 'Guide treasure')
    decisions = result.report.decisions
    old = next(d for d in decisions if d.source_id == 'player-0')
    assert old.owner == 'player' and old.state_version is not None
    assert old.selected and old.selection_reason == 'relevant' and old.truncated
    assert any(d.selection_reason == 'recent' and d.source_kind == 'history' for d in decisions)
    assert any(d.omission_reason == 'rank_limit' for d in decisions)
    assert not any(d.owner == 'guard' and d.source_kind in {'role','history','checkpoint'} for d in decisions)
    assert 'own private memory' not in str(result.request['player_history'])
    assert 'UNSELECTED_PRIVATE' not in str(result.request)
    assert 'ancient treasure secret' not in json.dumps(result.report.to_dict())


def test_public_engine_inspects_actual_clock_program_preferences_and_validates():
    from types import SimpleNamespace
    from storyloop_harness import TurnEngine, TurnInput
    from storyloop_harness.testing import OfflineModel
    from storyloop_harness.runtime.story_clock import StoryClock
    from storyloop_harness.runtime.player_preferences import player_preferences_scope
    store, base = fixture()
    package = replace(base, story_blueprint=None)
    engine = TurnEngine(store, package, OfflineModel(), clock=StoryClock(8),
        program=SimpleNamespace(current_action_context=lambda snapshot: {'scene':'test-scene', 'goal':'test-goal'}),
        context_budget=ContextBudget(output_tokens=321))
    before = store.load('game')
    turn = TurnInput('game','Guide hello','inspect',package.version)
    with player_preferences_scope(('patient style',)):
        result = engine.project_context(turn)
    assert result.request['time_period'] is not None
    assert result.request['current_story_scene'] == 'test-scene'
    assert result.request['player_style_preferences'] == ['patient style']
    assert result.report.output_tokens == 321
    assert store.load('game') == before
    with pytest.raises(ValueError, match='version'):
        engine.project_context(replace(turn, scenario_version='wrong'))


def test_required_overflow_precedes_model_and_writes():
    async def run():
        from storyloop_harness import TurnEngine, TurnInput
        store, package = fixture()
        class NoModel:
            async def __call__(self, *args, **kwargs):
                raise AssertionError('must not invoke model')
        before = store.load('game')
        engine = TurnEngine(store, package, NoModel(), context_budget=ContextBudget(context_window_tokens=2000,output_tokens=100))
        with pytest.raises(ContextBudgetExceeded):
            await engine.run_turn(TurnInput('game','hello','overflow',package.version))
        assert store.load('game') == before
        assert not store.event_exists('game','overflow:input')

    asyncio.run(run())


def test_generator_rechecks_mutated_request_and_passes_one_effective_cap():
    async def run():
        from types import SimpleNamespace
        from storyloop_harness.agents.scene_turn import SingleSceneGenerator
        from scene_fixtures import a_turn
        store, package = fixture()
        context = SceneContextProjector(store, package, context_budget=ContextBudget(output_tokens=321)).project(store.load('game'),'hello')
        captures = []
        class Capture:
            async def __call__(self, prompt, **kwargs):
                captures.append((prompt,kwargs))
                return SimpleNamespace(metadata=a_turn())
        generator = SingleSceneGenerator(Capture(),package)
        await generator.generate('game',context)
        assert captures[0][1]['context_budget'] == context.budget
        assert captures[0][1]['max_completion_tokens'] == 321
        assert 'max_tokens' not in captures[0][1]
        context.request['player_action'] = 'mutated private text ' * 2000
        with pytest.raises(ContextBudgetExceeded) as error:
            await generator.generate('game',context)
        assert len(captures) == 1
        assert 'mutated private text' not in str(error.value)

    asyncio.run(run())


def test_generated_identity_checkpoint_and_singleton_references_are_precise():
    from storyloop_harness.core.contracts import AgentContextCheckpoint
    store, package = fixture()
    snapshot = store.load('game')
    snapshot.data['actor_profiles'] = {'guide': {'name':'Generated Guide', 'role_card':'PRIVATE_ROLE ' * 200}}
    store.save_agent_context_checkpoint('game', 'guide', -1, AgentContextCheckpoint(0, 'PRIVATE_SUMMARY ' * 200))
    result = SceneContextProjector(store, package, max_responders=1).project(snapshot,'guide hello')
    role = next(d for d in result.report.decisions if d.source_kind == 'role')
    checkpoint = next(d for d in result.report.decisions if d.source_kind == 'checkpoint')
    assert role.source_id == 'snapshot:actor_profiles:guide:role_card' and role.owner == 'guide'
    assert role.state_version == snapshot.version and role.truncated
    assert checkpoint.source_id == 'guide:0' and checkpoint.state_version == 0 and checkpoint.truncated
    name = next(d for d in result.report.decisions if d.field == 'npc_contexts.0.name')
    assert name.source_id == 'snapshot:actor_profiles:guide:name'
    action = next(d for d in result.report.decisions if d.field == 'player_action')
    assert action.source_id == 'turn_input:player_text'
    assert 'PRIVATE_ROLE' not in json.dumps(result.report.to_dict())
    assert 'PRIVATE_SUMMARY' not in json.dumps(result.report.to_dict())


def test_recent_zero_and_excerpt_limit_do_not_select_all_history():
    from storyloop_harness.context.history import history_selection
    from storyloop_harness.core.contracts import AgentContextEntry
    entries = [AgentContextEntry(i,f'e-{i}','scene','long text ' * 100,i) for i in range(5)]
    rows = list(history_selection(entries,'missing',recent=0,older=1,excerpt_chars=40))
    assert sum(selected for _,_,selected,_ in rows) == 1
    assert all(len(row['text']) == 40 for _,row,_,_ in rows)


def test_prompt_milestone_omission_preserves_backend_eligibility():
    from storyloop_harness.world.story_blueprint import StoryMilestone
    store, base = fixture()
    milestones = [StoryMilestone(id=f'step_{i}',day=1,cue='Synthetic milestone ' * 50,source_ref='private ref',required=True) for i in range(6)]
    blueprint = base.story_blueprint.model_copy(update={'milestones':milestones})
    package = replace(base,story_blueprint=blueprint)
    eligible = blueprint.active_milestones(1,{})
    result = SceneContextProjector(store,package,context_budget=ContextBudget(max_material_tokens=3500)).project(store.load('game'),'hello')
    assert len(result.request['source_story_milestones']) < len(eligible)
    assert blueprint.active_milestones(1,{}) == eligible
    assert any(d.source_kind == 'milestone' and d.omission_reason == 'material_budget' for d in result.report.decisions)


@pytest.mark.parametrize('source', [False,True])
def test_full_fit_matches_canonical_request_and_accounts_for_complete_envelope(source):
    from storyloop_harness.context.budget import estimate_scene_request
    from storyloop_harness.agents.scene_prompt import serialize_scene_request
    from storyloop_harness.core.token_budget import estimate_tokens
    store,base = fixture()
    package = base if source else replace(base,story_blueprint=None)
    budget = ContextBudget(output_tokens=123,safety_margin_tokens=17)
    result = SceneContextProjector(store,package,context_budget=budget).project(store.load('game'),'escaped \" text')
    report = estimate_scene_request(result.request,package,budget)
    assert result.report.total_tokens == report.total_tokens
    assert report.system_tokens > 0 and report.schema_tokens > 0 and report.transport_tokens >= 512
    assert report.material_tokens > estimate_tokens(serialize_scene_request(result.request))
    assert report.total_tokens == sum((report.system_tokens,report.material_tokens,report.schema_tokens,report.transport_tokens,123,17))
    assert not any(d.omission_reason in {'material_budget','context_budget'} for d in result.report.decisions)


def test_inspection_and_actual_generator_request_match_with_runtime_collaborators():
    async def run():
        from types import SimpleNamespace
        from storyloop_harness import TurnEngine,TurnInput
        from storyloop_harness.runtime.story_clock import StoryClock
        from storyloop_harness.runtime.player_preferences import player_preferences_scope
        from scene_fixtures import a_turn
        store,base = fixture()
        package=replace(base,story_blueprint=None)
        calls=[]
        class Capture:
            async def __call__(self,messages,**kwargs):
                calls.append(messages)
                return SimpleNamespace(metadata=a_turn())
        engine=TurnEngine(store,package,Capture(),clock=StoryClock(8),program=SimpleNamespace(current_action_context=lambda snapshot:{'goal':'find boat'}))
        turn=TurnInput('game','Hello','match',package.version)
        with player_preferences_scope(('patient style',)):
            inspected=engine.project_context(turn)
            await engine.run_turn(turn)
        assert json.loads(calls[0][-1]['content'][0]['text']) == inspected.request
        assert 'report' not in json.loads(calls[0][-1]['content'][0]['text'])
    asyncio.run(run())


def test_large_source_hints_yield_before_recent_history_and_role_identity():
    from storyloop_harness.core.contracts import WorldEvent,Observation
    store,base = fixture()
    for i in range(3):
        event=WorldEvent(f'recent-event-{i}','synthetic',None,None,i,())
        store.commit('game',store.load('game').version,event,
            (Observation(f'recent-{i}',event.event_id,'player','scene','Recent player experience '+str(i),i),),())
    fact=base.story_blueprint.facts[0].model_copy(update={'text':'Large optional fact '*80})
    package=replace(base,story_blueprint=base.story_blueprint.model_copy(update={'facts':[fact]}))
    result=SceneContextProjector(store,package,context_budget=ContextBudget(max_material_tokens=3000)).project(store.load('game'),'hello')
    assert len(result.request['player_history']) == 3
    assert result.request['source_story_facts'] == []
    assert all(item['role_card'] for item in result.request['npc_contexts'])


def test_clipped_fields_carry_field_limit_reason_separately_from_selection():
    store,package=fixture()
    snapshot=store.load('game')
    snapshot.data['actor_profiles']={'guide':{'role_card':'Private long role '*120}}
    result=SceneContextProjector(store,package,max_responders=1).project(snapshot,'guide hello')
    role=next(d for d in result.report.decisions if d.source_kind=='role')
    assert role.selected and role.truncated and role.omission_reason == 'field_limit'


def test_window_ceiling_trims_with_distinct_reason_and_preserves_required_material():
    store,package=fixture()
    full=SceneContextProjector(store,package).project(store.load('game'),'hello')
    budget=replace(full.budget,context_window_tokens=full.report.total_tokens-1)
    result=SceneContextProjector(store,package,context_budget=budget).project(store.load('game'),'hello')
    assert result.report.total_tokens <= budget.context_window_tokens
    assert any(d.omission_reason == 'context_budget' for d in result.report.decisions)
    assert result.request['player_action'] == 'hello'
    assert result.focus_actor_ids == full.focus_actor_ids
    assert result.request['source_resolutions'] == full.request['source_resolutions']


def test_public_inspection_rejects_wrong_saved_identity_and_emits_no_content():
    from unittest.mock import patch
    from storyloop_harness import TurnEngine,TurnInput
    from storyloop_harness.testing import OfflineModel
    store,package=fixture()
    engine=TurnEngine(store,package,OfflineModel())
    turn=TurnInput('game','hello','inspect',package.version)
    snapshot=store.load('game')
    snapshot.data['scenario']['id']='another-scenario'
    with patch.object(store,'load',return_value=snapshot), patch.object(store,'commit',side_effect=AssertionError('must not commit')):
        with pytest.raises(ValueError,match='identity/version'):
            engine.project_context(turn)


def test_rejection_telemetry_is_numeric_and_contains_no_source_material():
    async def run():
        from contextlib import contextmanager
        from storyloop_harness import TurnEngine,TurnInput
        from storyloop_harness.testing import OfflineModel
        records=[]
        class Span:
            def metric(self,name,value):
                assert type(value) is float
                records.append((name,value))
            def update(self,**kwargs):
                raise AssertionError('content update unexpected')
        class Telemetry:
            capture_content=False
            @contextmanager
            def span(self,name,metadata,**kwargs):
                assert kwargs.get('input') is None
                assert 'decisions' not in metadata
                yield Span()
        store,package=fixture()
        engine=TurnEngine(store,package,OfflineModel(),telemetry=Telemetry(),context_budget=ContextBudget(context_window_tokens=2000,output_tokens=100))
        with pytest.raises(ContextBudgetExceeded):
            await engine.run_turn(TurnInput('game','PRIVATE_ACTION','overflow',package.version))
        assert ('story.context.rejected',1.0) in records
        assert any(name == 'story.context.estimated_total_tokens' for name,_ in records)
        assert 'PRIVATE_ACTION' not in str(records)
    asyncio.run(run())
