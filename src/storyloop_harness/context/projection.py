"""Ordinary scene projection shared by runtime and developer inspection."""
from storyloop_harness.context import ContextBudget, ContextProjection
from storyloop_harness.context.selection import Selection
from storyloop_harness.context.history import history_selection
from storyloop_harness.context.policy import ContextPolicy, policy_spec
from storyloop_harness.core.contracts import Snapshot
from storyloop_harness.core.store_port import GameStore
from storyloop_harness.runtime.player_preferences import current_player_preferences
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.world.status_fields import project_status_fields
from storyloop_harness.core.actions import _read
from storyloop_harness.runtime.action_resolution import overlaps

class SceneContextProjector:
    """Select scoped candidates, retaining IDs through clipping and fitting."""

    def __init__(self, store: GameStore, package: ScenarioPackage,
                 *, max_responders: int = 3, context_budget: ContextBudget | None = None,
                 clock: StoryClock | None = None, program=None,
                 context_policy: ContextPolicy | None = None) -> None:
        self.store = store
        self.package = package
        self.max_responders = max_responders
        self.context_budget = context_budget or ContextBudget()
        self.clock = clock
        self.program = program
        self.context_policy, self.policy_spec = policy_spec(context_policy)

    def project(self, snapshot: Snapshot, player_text: str) -> ContextProjection:
        selection = Selection()
        def origin(path, value, *sources, owner=None):
            for source in sources:
                kind = source.split(':', 1)[0]
                selection.add(kind, source, path, value, owner=owner,
                    version=snapshot.version if kind == 'snapshot' else None)
        actors = snapshot.data.get("actors", {})
        generated_profiles = snapshot.data.get("actor_profiles", {})
        if not isinstance(generated_profiles, dict):
            generated_profiles = {}
        def actor_name(actor_id: str) -> str:
            profile = generated_profiles.get(actor_id)
            return (profile.get("name") if isinstance(profile, dict)
                    and isinstance(profile.get("name"), str) else
                    self.package.actor_names[actor_id])
        def actor_card(actor_id: str) -> str:
            profile = generated_profiles.get(actor_id)
            return (profile.get("role_card") if isinstance(profile, dict)
                    and isinstance(profile.get("role_card"), str)
                    and profile["role_card"].strip() else
                    self.package.worldbook.get(dict(self.package.actor_cards)[actor_id], actor_id).text)
        player = actors.get("player", {}) if isinstance(actors, dict) else {}
        location = player.get("location") if isinstance(player, dict) else None
        nearby = tuple(actor_id for actor_id, _ in self.package.actor_cards
                       if isinstance(actors.get(actor_id), dict)
                       and actors[actor_id].get("location") == location)
        mentioned = [actor_id for actor_id, _ in self.package.actor_cards
                     if (actor_id in player_text or actor_name(actor_id) in player_text)
                     and (self.package.story_blueprint is None or actor_id in nearby)]
        recent_inputs = self.store.player_inputs_for(snapshot.game_id, limit=4)
        recent_targets = [actor_id for item in reversed(recent_inputs)
                          for actor_id in item.target_ids if actor_id in nearby]
        focus = tuple(dict.fromkeys((*mentioned, *recent_targets, *nearby)))[:self.max_responders]

        responder_candidates = tuple(dict.fromkeys((*mentioned, *recent_targets, *nearby)))
        for actor_id in responder_candidates:
            reason = 'mentioned' if actor_id in mentioned else 'recent_target' if actor_id in recent_targets else 'nearby'
            selection.add('responder', actor_id, ('candidate_responders',), actor_id, reason=reason, group=True,
                selected=actor_id in focus, omission=None if actor_id in focus else 'responder_limit', owner=actor_id)
        player_checkpoint = self.store.agent_context_checkpoint(snapshot.game_id, 'player')
        player_history = []
        self._collect_history(selection, snapshot, 'player', player_checkpoint,
            player_text, ('player_history',), window=self.policy_spec.player)
        npc_contexts = []
        for index, actor_id in enumerate(focus):
            role_card = actor_card(actor_id).strip()
            if not role_card:
                raise ValueError('responder role card is required')
            profile = generated_profiles.get(actor_id)
            generated = isinstance(profile, dict) and isinstance(profile.get('role_card'), str) and bool(profile['role_card'].strip())
            role_id = f'snapshot:actor_profiles:{actor_id}:role_card' if generated else dict(self.package.actor_cards)[actor_id]
            path = ('npc_contexts', index)
            generated_name = isinstance(profile, dict) and isinstance(profile.get('name'), str)
            name_source = f'snapshot:actor_profiles:{actor_id}:name' if generated_name else f'package:actor_names:{actor_id}'
            selection.add('identity', name_source, (*path, 'name'), actor_name(actor_id),
                owner=actor_id, version=snapshot.version if generated_name else None)
            selection.add('role', role_id, (*path, 'role_card'), role_card, role_card[:1200],
                owner=actor_id, version=snapshot.version if generated else None,
                optional=True, floor=role_card[:160])
            checkpoint = self.store.agent_context_checkpoint(snapshot.game_id, actor_id)
            if self.policy_spec.use_checkpoint:
                self._collect_checkpoint(selection, snapshot, actor_id, checkpoint, (*path, 'own_summary'))
            self._collect_history(selection, snapshot, actor_id, checkpoint, player_text,
                (*path, 'own_history'), window=self.policy_spec.npc)
            npc_contexts.append({'id': actor_id, 'name': actor_name(actor_id),
                'role_card': '', 'own_summary': '', 'own_history': []})
        if self.policy_spec.use_checkpoint:
            self._collect_checkpoint(selection, snapshot, 'player', player_checkpoint, ('player_summary',))
        campaign = snapshot.data.get("campaign")
        story_context = (self.program.current_action_context(snapshot)
                         if self.program is not None else {})
        visible = [item for item in self.store.observations_for(snapshot.game_id, 'player', limit=6)
                   if item.channel in {'scene', 'narration', 'dialogue'}]
        for index, item in enumerate(visible):
            selection.add('observation', item.observation_id, ('recent_visible_beats',),
                item.content, item.content[-900:], owner='player', group=True, optional=True,
                reason='recent', selected=index >= max(0, len(visible)-4),
                omission='item_limit' if index < max(0, len(visible)-4) else None)
        for item in recent_inputs:
            selection.add('player_input', item.event_id, ('recent_player_actions',), item.text,
                item.text[:200], reason='recent', owner='player', group=True, optional=True)
        lore = self.package.worldbook.visible_lore('player', limit=None)
        for index, item in enumerate(lore):
            selection.add('worldbook_lore', item.entry_id, ('public_setting',), item.text,
                item.text[:650], reason='setting', selected=index < 4,
                omission='item_limit' if index >= 4 else None, optional=True, group=True)
        rules = self.package.worldbook.visible_rules('player', limit=None)
        for item in rules:
            selection.add('worldbook_rule', item.entry_id, ('public_rules',), item.text, group=True)
        request: dict[str, object] = {
            "player_action": player_text, "presentation_mode": self.package.presentation_mode,
            "location": location, "tick": snapshot.tick,
            "day": campaign.get("day") if isinstance(campaign, dict) else None,
            "time_period": self.clock.period(snapshot.tick) if self.clock else None,
            "player_profile": snapshot.data.get("player_profile", {}),
            "nearby_people": {actor_id: actor_name(actor_id) for actor_id in nearby},
            "candidate_responders": list(focus),
            "public_setting": [],
            "public_rules": [],
            "player_summary": "",
            "player_history": player_history,
            "npc_contexts": npc_contexts,
            "current_story_scene": story_context.get("scene", ""),
            "current_story_goal": story_context.get("goal", ""),
            "story_anchors": story_context.get("anchors", []),
            "recent_player_actions": [],
            "recent_visible_beats": [],
            "player_style_preferences": list(current_player_preferences()),
        }
        origin(('player_action',), player_text, 'turn_input:player_text')
        origin(('presentation_mode',), request['presentation_mode'], 'package:presentation_mode')
        origin(('location',), location, 'snapshot:actors:player:location', owner='player')
        origin(('tick',), snapshot.tick, 'snapshot:tick')
        origin(('player_profile',), request['player_profile'], 'snapshot:player_profile', owner='player')
        if self.clock:
            origin(('time_period',), request['time_period'], 'snapshot:tick',
                   'clock:ticks_per_day', 'clock:PERIODS')
        if request['player_style_preferences']:
            origin(('player_style_preferences',), request['player_style_preferences'],
                   'preferences:current_player_preferences', owner='player')
        if self.package.story_blueprint is None:
            if request['day'] is not None:
                origin(('day',), request['day'], 'snapshot:campaign:day')
            for actor_id in nearby:
                profile = generated_profiles.get(actor_id)
                source = (f'snapshot:actor_profiles:{actor_id}:name' if isinstance(profile, dict)
                          and isinstance(profile.get('name'), str) else f'package:actor_names:{actor_id}')
                origin(('nearby_people', actor_id), actor_name(actor_id), source,
                       f'snapshot:actors:{actor_id}:location', 'snapshot:actors:player:location', owner=actor_id)
            for field, source_field in (('current_story_scene', 'scene'),
                                        ('current_story_goal', 'goal'), ('story_anchors', 'anchors')):
                if request[field]:
                    origin((field,), request[field], f'program:current_action_context:{source_field}')
        updatable = {field.field_id: field for field in self.package.status_fields
                     if field.automatically_updated}
        request['actions'] = []
        for rule in self.package.action_rules.values():
            row = {'id': rule.action_id, 'label': rule.label,
                   'available': location == rule.location
                   and _read(snapshot.data, rule.when_path) == rule.when_value}
            index = len(request['actions'])
            request['actions'].append(row)
            for key, value in row.items():
                origin(('actions', index, key), value,
                       f'package:action_rules:{rule.action_id}:{key}')
            origin(('actions', index, 'available'), row['available'],
                   'snapshot:actors:player:location',
                   f'snapshot:{":".join(rule.when_path)}')
        request['mutable_state'] = []
        reserved = [field.path for field in self.package.status_fields if field.automatically_updated]
        reserved.extend([('story_progress',), ('player_knowledge',)])
        for field in self.package.mutable_fields:
            if any(overlaps(field.path, path) for path in reserved):
                continue
            current = _read(snapshot.data, field.path)
            row = {'path': list(field.path), 'current': current,
                   'next_values': list(field.next_values(current))}
            index = len(request['mutable_state'])
            request['mutable_state'].append(row)
            for key, value in row.items():
                origin(('mutable_state', index, key), value,
                       f'package:mutable_fields:{":".join(field.path)}:{key}')
            origin(('mutable_state', index, 'current'), current,
                   f'snapshot:{":".join(field.path)}')
            origin(('mutable_state', index, 'next_values'), row['next_values'],
                   f'snapshot:{":".join(field.path)}')
        request["status_fields"] = [
            {**row, "description": updatable[row["id"]].description,
             "max_delta": updatable[row["id"]].max_delta}
            for row in project_status_fields(self.package.status_fields, snapshot.data)
            if row["id"] in updatable
        ]
        for index, row in enumerate(request['status_fields']):
            spec = updatable[row['id']]
            for key, value in row.items():
                source_field = {'id': 'field_id', 'min': 'minimum', 'max': 'maximum'}.get(key, key)
                sources = ([f'snapshot:{":".join(spec.path)}'] if key == 'value'
                           else [f'package:status_fields:{spec.field_id}:{source_field}'])
                origin(('status_fields', index, key), value, *sources)
            if spec.when_path:
                origin(('status_fields', index, 'id'), row['id'],
                       f'snapshot:{":".join(spec.when_path)}', f'package:status_fields:{spec.field_id}:when_equals')
        blueprint = self.package.story_blueprint
        if blueprint is not None:
            knowledge = snapshot.data.get("player_knowledge", {})
            if not isinstance(knowledge, dict):
                knowledge = {}
            seen_ids = knowledge.get("seen_actor_ids", [])
            named_ids = knowledge.get("named_actor_ids", [])
            request["player_identity_knowledge"] = {
                "seen_actor_ids": seen_ids if isinstance(seen_ids, list) else [],
                "named_actor_ids": named_ids if isinstance(named_ids, list) else [],
            }
            for key, value in request['player_identity_knowledge'].items():
                origin(('player_identity_knowledge', key), value, f'snapshot:player_knowledge:{key}', owner='player')
            day = snapshot.tick // self.clock.ticks_per_day + 1 if self.clock else 1
            request["day"] = day
            origin(('day',), day, *(['snapshot:tick', 'clock:ticks_per_day'] if self.clock
                                  else ['projection:source_day_default']))
            setup = snapshot.data.get("story_setup", {})
            tone_id = setup.get("tone") if isinstance(setup, dict) else None
            tone = next((item.guidance for item in blueprint.setup.tone_options
                         if item.id == tone_id), "")
            request['source_story_facts'] = []
            self._collect_facts(selection, blueprint, day, player_text)
            request["source_resolutions"] = blueprint.source_resolutions
            if blueprint.source_resolutions:
                origin(('source_resolutions',), blueprint.source_resolutions, 'package:story_blueprint:source_resolutions')
            progress = snapshot.data.get("story_progress", {})
            completed = progress.get("completed", {}) if isinstance(progress, dict) else {}
            request['source_story_milestones'] = []
            active = blueprint.active_milestones(day, completed if isinstance(completed, dict) else {})
            active_by_id = {item['id']: item for item in active}
            ordered_steps = [next(step for step in blueprint.milestones if step.id == item['id']) for item in active]
            ordered_steps.extend(step for step in blueprint.milestones if step.id not in active_by_id)
            for step in ordered_steps:
                selected = step.id in active_by_id
                omission = ('completed' if isinstance(completed, dict) and completed.get(step.id, False)
                            else 'future' if step.day > day else 'inactive' if step.day < day and not step.required else 'item_limit')
                value = active_by_id.get(step.id, {'id': step.id, 'source_day': step.day,
                                                'cue': step.cue, 'overdue': step.day < day})
                selection.add('milestone', step.id, ('source_story_milestones',), value,
                    selected=selected, reason='current' if step.day == day else 'overdue',
                    omission=None if selected else omission, optional=True, group=True)
            request["source_story_tone"] = tone
            if isinstance(setup, dict) and 'tone' in setup:
                origin(('source_story_tone',), tone, 'snapshot:story_setup:tone')
            if any(item.id == tone_id for item in blueprint.setup.tone_options):
                origin(('source_story_tone',), tone, f'package:story_blueprint:setup:tone_options:{tone_id}:guidance')
            request["public_cast"] = [
                {"id": actor_id, "name": actor_name(actor_id),
                 "seen_by_player": actor_id in seen_ids,
                 "name_known_to_player": actor_id in named_ids,
                 "appearance": (generated_profiles.get(actor_id, {}).get("public_profile", "")
                                if isinstance(generated_profiles.get(actor_id), dict) else "")}
                for actor_id in nearby
            ]
            for index, row in enumerate(request['public_cast']):
                actor_id = row['id']
                profile = generated_profiles.get(actor_id)
                origin(('public_cast', index, 'id'), actor_id, f'package:actor_cards:{actor_id}',
                       f'snapshot:actors:{actor_id}:location', 'snapshot:actors:player:location', owner=actor_id)
                name_source = (f'snapshot:actor_profiles:{actor_id}:name' if isinstance(profile, dict)
                               and isinstance(profile.get('name'), str) else f'package:actor_names:{actor_id}')
                origin(('public_cast', index, 'name'), row['name'], name_source, owner=actor_id)
                origin(('public_cast', index, 'appearance'), row['appearance'],
                       f'snapshot:actor_profiles:{actor_id}:public_profile', owner=actor_id)
                for field, key in (('seen_by_player', 'seen_actor_ids'), ('name_known_to_player', 'named_actor_ids')):
                    origin(('public_cast', index, field), row[field], f'snapshot:player_knowledge:{key}', owner=actor_id)
            request["nearby_people"] = {}
            request["current_story_scene"] = ""
            request["current_story_goal"] = ""
            request["story_anchors"] = []
        if isinstance(campaign, dict):
            request["player_recorded_choices"] = campaign.get("choices", {})
            request["player_sent_messages"] = {
                key: value for key, value in campaign.get("messages", {}).items()
                if isinstance(key, str) and isinstance(value, str) and value
            } if isinstance(campaign.get("messages"), dict) else {}
            for field, source_field in (('player_recorded_choices', 'choices'), ('player_sent_messages', 'messages')):
                if request[field]:
                    origin((field,), request[field], f'snapshot:campaign:{source_field}', owner='player')

        return selection.fit(request, self.package, self.context_budget, nearby, focus,
                             policy_id=self.context_policy.value,
                             history_scope=('recipient-history-after-checkpoint' if self.policy_spec.use_checkpoint
                                            else 'recipient-history-all-committed'))

    def _collect_checkpoint(self, selection, snapshot, owner, checkpoint, path):
        if checkpoint.summary:
            scenario = snapshot.data.get('scenario', {})
            if (not isinstance(scenario, dict) or checkpoint.scenario_id != scenario.get('id')
                    or checkpoint.scenario_version != scenario.get('version')
                    or checkpoint.through_version > snapshot.version):
                raise ValueError('context checkpoint scenario does not match game')
            selection.add('checkpoint', f'{owner}:{checkpoint.through_version}', path,
                checkpoint.summary, checkpoint.summary[:1200], reason='summary',
                owner=owner, version=checkpoint.through_version, optional=True)

    def _collect_history(self, selection, snapshot, owner, checkpoint, query, path,
                         *, window):
        cursor = checkpoint.through_version if self.policy_spec.use_checkpoint else -1
        entries = self.store.agent_context_entries(snapshot.game_id, owner, cursor)
        for entry, value, selected, reason in history_selection(entries, query,
                recent=window.recent, older=window.older, excerpt_chars=window.excerpt_chars):
            raw = {'channel': entry.channel, 'text': entry.content, 'tick': entry.tick}
            selection.add('history', entry.entry_id, path, raw, value,
                reason=reason, selected=selected, omission=None if selected else 'rank_limit',
                owner=owner, version=entry.state_version, optional=True, group=True)

    @staticmethod
    def _collect_facts(selection, blueprint, day, query):
        # Only public/system author material is inspected; private facts are outside scope.
        visible = [fact for fact in blueprint.facts if fact.visibility in {'public', 'system'}]
        groups = (([fact for fact in visible if not fact.active_days], 6, 'general'),
                  ([fact for fact in visible if day in fact.active_days], 8, 'current'),
                  ([fact for fact in visible if fact.active_days and day not in fact.active_days
                    and any(word.casefold() in query.casefold() for word in fact.keywords)], 4, 'relevant'))
        chosen = {}
        reasons = {}
        for facts, limit, reason in groups:
            for index, fact in enumerate(facts):
                reasons[fact.id] = reason
                if index < limit and len(chosen) < 16:
                    chosen[fact.id] = fact
        # Keep canonical category order while retaining omitted scoped candidates.
        ordered = [*chosen.values(), *(fact for fact in visible if fact.id not in chosen)]
        for fact in ordered:
            selected = fact.id in chosen
            selection.add('source_fact', fact.id, ('source_story_facts',), fact.text,
                reason=reasons.get(fact.id, 'inactive'), selected=selected,
                omission=None if selected else 'item_limit' if fact.id in reasons else 'inactive',
                optional=True, group=True)
