"""Authoritative action validation before narrative consequences are persisted."""
from storyloop_harness.agents.scene_schema import NarrationBeat, ProposedAction, SceneTurn
from storyloop_harness.core.actions import _read
from storyloop_harness.core.contracts import Effect, Snapshot
from storyloop_harness.core.open_actions import validate_open_effects


def overlaps(left, right):
    return left[:len(right)] == right or right[:len(left)] == left


def resolve_action(before: Snapshot, package, plan: SceneTurn) -> SceneTurn:
    if plan.decision.intent != 'action':
        return plan
    proposal = plan.action
    reason = None
    effects = tuple(Effect(tuple(item.path), item.value) for item in proposal.effects)
    sensory = proposal.sensory
    action_id = plan.decision.action_id
    rule = package.action_rules.get(action_id) if action_id is not None else None
    if proposal.status != 'occurred':
        reason = '这个行动尚未完成，不能确认其结果。'
    elif action_id is not None and rule is None:
        reason = '这个行动没有对应的已声明规则。'
    elif rule is not None:
        player = before.data.get('actors', {}).get('player', {})
        if player.get('location') != rule.location:
            reason = '你当前不在这个行动所需的位置。'
        elif _read(before.data, rule.when_path) != rule.when_value:
            reason = '当前状态已不允许重复完成这个行动。'
        effects = rule.effects
        sensory = rule.sensory
    else:
        try:
            validate_open_effects(effects, package.mutable_fields, before.data)
        except ValueError:
            reason = '这个行动提议的状态变化不符合已声明的限制。'
    # Numeric updates, story progress and identity knowledge have distinct owners.
    # Reserve the entire owned subtree so ancestor writes cannot bypass collision checks.
    reserved = [field.path for field in package.status_fields if field.automatically_updated]
    reserved.extend([('story_progress',), ('player_knowledge',)])
    if any(overlaps(effect.path, path) for effect in effects for path in reserved):
        reason = '这个行动的状态变化与自动结算字段冲突。'
    if reason is not None:
        return plan.model_copy(update={
            'beats': [NarrationBeat(kind='narration', text=reason)],
            'action': ProposedAction(status='blocked', player_result=reason),
            'memories': [], 'status_changes': [], 'options': [],
            'encounters': [], 'milestones_fulfilled': [],
        })
    action = ProposedAction(status='occurred', player_result=sensory or proposal.player_result,
        sensory=sensory, effects=[] if rule is not None else proposal.effects)
    # Known rules retain only their ID and canonical result in the resolved plan.
    # Trusted authored values never pass through the model's scalar proposal schema.
    # Authored results are the canonical action scene; a model cannot negate them.
    if rule is not None:
        return plan.model_copy(update={'action': action,
            'beats': [NarrationBeat(kind='narration', text=sensory)],
            'memories': [], 'encounters': []})
    return plan.model_copy(update={'action': action})
