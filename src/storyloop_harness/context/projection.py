"""Ordinary scene projection shared by runtime and developer inspection."""
from storyloop_harness.context import ContextBudget, ContextProjection
from storyloop_harness.context.selection import Selection
from storyloop_harness.context.history import history_selection
from storyloop_harness.core.contracts import Snapshot
from storyloop_harness.core.store_port import GameStore
from storyloop_harness.runtime.player_preferences import current_player_preferences
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.world.status_fields import project_status_fields

class SceneContextProjector:
    """Select scoped candidates, retaining IDs through clipping and fitting."""

    def __init__(self, store: GameStore, package: ScenarioPackage,
                 *, max_responders: int = 3, context_budget: ContextBudget | None = None,
                 clock: StoryClock | None = None, program=None) -> None:
        self.store = store
        self.package = package
        self.max_responders = max_responders
        self.context_budget = context_budget or ContextBudget()
        self.clock = clock
        self.program = program

    def project(self, snapshot: Snapshot, player_text: str) -> ContextProjection:
        selection = Selection()
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
            player_text, ('player_history',), recent=10, older=4, excerpt_chars=700)
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
            self._collect_checkpoint(selection, snapshot, actor_id, checkpoint, (*path, 'own_summary'))
            self._collect_history(selection, snapshot, actor_id, checkpoint, player_text,
                (*path, 'own_history'), recent=8, older=3, excerpt_chars=400)
            npc_contexts.append({'id': actor_id, 'name': actor_name(actor_id),
                'role_card': '', 'own_summary': '', 'own_history': []})
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
        updatable = {field.field_id: field for field in self.package.status_fields
                     if field.automatically_updated}
        request["status_fields"] = [
            {**row, "description": updatable[row["id"]].description,
             "max_delta": updatable[row["id"]].max_delta}
            for row in project_status_fields(self.package.status_fields, snapshot.data)
            if row["id"] in updatable
        ]
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
            day = snapshot.tick // self.clock.ticks_per_day + 1 if self.clock else 1
            request["day"] = day
            setup = snapshot.data.get("story_setup", {})
            tone_id = setup.get("tone") if isinstance(setup, dict) else None
            tone = next((item.guidance for item in blueprint.setup.tone_options
                         if item.id == tone_id), "")
            request['source_story_facts'] = []
            self._collect_facts(selection, blueprint, day, player_text)
            request["source_resolutions"] = blueprint.source_resolutions
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
            request["public_cast"] = [
                {"id": actor_id, "name": actor_name(actor_id),
                 "seen_by_player": actor_id in seen_ids,
                 "name_known_to_player": actor_id in named_ids,
                 "appearance": (generated_profiles.get(actor_id, {}).get("public_profile", "")
                                if isinstance(generated_profiles.get(actor_id), dict) else "")}
                for actor_id in nearby
            ]
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
        managed = {item.path[0] for item in selection.items if item.path}
        # Singleton references identify package/snapshot fields, never their prose.
        for field, value in request.items():
            if field not in managed and value not in ('', [], {}, None):
                kind = ('turn_input' if field == 'player_action' else 'preferences' if field == 'player_style_preferences'
                        else 'program' if field in {'current_story_scene', 'current_story_goal', 'story_anchors'}
                        else 'package' if field in {'presentation_mode', 'source_resolutions', 'source_story_tone'} else 'snapshot')
                source_id = 'turn_input:player_text' if field == 'player_action' else f'{kind}:{field}'
                selection.add(kind, source_id, (field,), value,
                    version=snapshot.version if kind == 'snapshot' else None)

        return selection.fit(request, self.package, self.context_budget, nearby, focus)

    def _collect_checkpoint(self, selection, snapshot, owner, checkpoint, path):
        if checkpoint.summary:
            selection.add('checkpoint', f'{owner}:{checkpoint.through_version}', path,
                checkpoint.summary, checkpoint.summary[:1200], reason='summary',
                owner=owner, version=checkpoint.through_version, optional=True)

    def _collect_history(self, selection, snapshot, owner, checkpoint, query, path,
                         *, recent, older, excerpt_chars):
        entries = self.store.agent_context_entries(snapshot.game_id, owner, checkpoint.through_version)
        for entry, value, selected, reason in history_selection(entries, query,
                recent=recent, older=older, excerpt_chars=excerpt_chars):
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
