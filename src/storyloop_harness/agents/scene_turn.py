"""Story-first generation with a small, optional persistence sidecar."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from storyloop_harness.adapters.agentscope_message import Msg
from storyloop_harness.ports import ModelPort
from storyloop_harness.agents import scene_schema
from storyloop_harness.agents.scene_prompt import scene_system_prompt, serialize_scene_request

from storyloop_harness.core.store_port import GameStore
from storyloop_harness.adapters.telemetry import NoopTelemetry, Telemetry, session_id_for_game
from storyloop_harness.agents.openai_formatter import ThinkingSafeOpenAIChatFormatter
from storyloop_harness.core.contracts import AgentContextEntry, Snapshot
from storyloop_harness.core.token_budget import estimate_tokens
from storyloop_harness.runtime.player_preferences import current_player_preferences
from storyloop_harness.runtime.story_clock import StoryClock
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.world.status_fields import project_status_fields


@dataclass(frozen=True)
class SceneModelContext:
    request: dict[str, object]
    nearby_actor_ids: tuple[str, ...]
    focus_actor_ids: tuple[str, ...]


class SceneContextProjector:
    """Project recipient-scoped history for a bounded set of responders.

    Public cast descriptions may cover additional people, but structured replies
    and memories are restricted to focus_actor_ids with projected role context.
    Remaining T07 work includes configurable source-fact/milestone budgets and
    prepublication validation of oversized required context. A single model sees
    multiple NPC contexts; this does not provide hard isolation between calls.
    """

    def __init__(self, store: GameStore, package: ScenarioPackage,
                 *, max_responders: int = 3, context_window_tokens: int = 65536,
                 clock: StoryClock | None = None, program=None) -> None:
        self.store = store
        self.package = package
        self.max_responders = max_responders
        self.context_window_tokens = context_window_tokens
        self.clock = clock
        self.program = program

    @staticmethod
    def _history(entries: list[AgentContextEntry], query: str,
                 *, recent: int, older: int, excerpt_chars: int = 400) -> list[dict[str, object]]:
        """Retain recent context plus relevant earlier, actor-owned milestones."""
        def excerpt(content: str) -> str:
            if len(content) <= excerpt_chars:
                return content
            head = excerpt_chars // 2
            return content[:head] + " … " + content[-head:]

        terms = set()
        for word in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{3,}", query.casefold()):
            if re.search(r"[\u4e00-\u9fff]", word):
                terms.update(word[index:index + 2] for index in range(len(word) - 1))
            else:
                terms.add(word)
        earlier = entries[:-recent] if len(entries) > recent else []
        notable = {"private_message", "campaign_choice", "shared_experience",
                   "witnessed", "outgoing_message", "background"}
        ranked = sorted(enumerate(earlier), key=lambda pair: (
            sum(term in pair[1].content.casefold() for term in terms) * 4
            + (2 if pair[1].channel in notable else 0), pair[0],
        ), reverse=True)
        chosen = {index for index, _ in ranked[:older]}
        selected = [entry for index, entry in enumerate(earlier) if index in chosen]
        selected.extend(entries[-recent:])
        return [{"channel": entry.channel, "text": excerpt(entry.content), "tick": entry.tick}
                for entry in selected]

    def project(self, snapshot: Snapshot, player_text: str) -> SceneModelContext:
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
                    self.package.role_cards[actor_id])
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

        player_checkpoint = self.store.agent_context_checkpoint(snapshot.game_id, "player")
        player_history = self._history(
            self.store.agent_context_entries(
                snapshot.game_id, "player", player_checkpoint.through_version),
            player_text, recent=10, older=4, excerpt_chars=700,
        )
        npc_contexts: list[dict[str, object]] = []
        for actor_id in focus:
            role_card = actor_card(actor_id).strip()[:1200]
            if not role_card:
                raise ValueError("responder role card is required")
            checkpoint = self.store.agent_context_checkpoint(snapshot.game_id, actor_id)
            entries = self.store.agent_context_entries(snapshot.game_id, actor_id,
                                                       checkpoint.through_version)
            npc_contexts.append({
                "id": actor_id, "name": actor_name(actor_id),
                "role_card": role_card,
                "own_summary": checkpoint.summary[:1200],
                "own_history": self._history(entries, player_text, recent=8, older=3),
            })
        campaign = snapshot.data.get("campaign")
        story_context = (self.program.current_action_context(snapshot)
                         if self.program is not None else {})
        recent_visible = [item.content[-900:] for item in
                          self.store.observations_for(snapshot.game_id, "player", limit=6)
                          if item.channel in {"scene", "narration", "dialogue"}]
        request: dict[str, object] = {
            "player_action": player_text, "presentation_mode": self.package.presentation_mode,
            "location": location, "tick": snapshot.tick,
            "day": campaign.get("day") if isinstance(campaign, dict) else None,
            "time_period": self.clock.period(snapshot.tick) if self.clock else None,
            "player_profile": snapshot.data.get("player_profile", {}),
            "nearby_people": {actor_id: actor_name(actor_id) for actor_id in nearby},
            "candidate_responders": list(focus),
            "public_setting": [item.text[:650] for item in
                               self.package.worldbook.visible_lore("player", limit=4)],
            "public_rules": [item.text[:700] for item in
                             self.package.worldbook.visible_rules("player", limit=4)],
            "player_summary": player_checkpoint.summary[:1200],
            "player_history": player_history,
            "npc_contexts": npc_contexts,
            "current_story_scene": story_context.get("scene", ""),
            "current_story_goal": story_context.get("goal", ""),
            "story_anchors": story_context.get("anchors", []),
            "recent_player_actions": [item.text[:200] for item in recent_inputs],
            "recent_visible_beats": recent_visible[-4:],
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
            request["source_story_facts"] = blueprint.relevant_facts(day, player_text)
            request["source_resolutions"] = blueprint.source_resolutions
            progress = snapshot.data.get("story_progress", {})
            completed = progress.get("completed", {}) if isinstance(progress, dict) else {}
            request["source_story_milestones"] = blueprint.active_milestones(
                day, completed if isinstance(completed, dict) else {},
            )
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
                key: value[:200] for key, value in campaign.get("messages", {}).items()
                if isinstance(key, str) and isinstance(value, str) and value
            } if isinstance(campaign.get("messages"), dict) else {}
        budget = min(16000, max(4800, self.context_window_tokens * 2 // 3))
        while estimate_tokens(json.dumps(request, ensure_ascii=False)) > budget:
            if player_history:
                player_history.pop(0)
            elif any(item["own_history"] for item in npc_contexts):
                largest = max(npc_contexts, key=lambda item: len(item["own_history"]))
                largest["own_history"].pop(0)
            elif request["public_setting"]:
                request["public_setting"].pop()
            elif request["recent_visible_beats"]:
                request["recent_visible_beats"].pop(0)
            elif npc_contexts:
                if not any(len(item["role_card"]) > 160 for item in npc_contexts):
                    raise ValueError("configured context window cannot preserve responder role cards")
                for item in npc_contexts:
                    # Role identity is mandatory; never reduce a permitted
                    # responder's context to an empty card to fit the budget.
                    if len(item["role_card"]) > 160:
                        item["role_card"] = item["role_card"][:max(160, len(item["role_card"]) // 2)]
            else:
                raise ValueError("player action exceeds the configured context window")
        return SceneModelContext(request, nearby, focus)


class SingleSceneGenerator:
    def __init__(self, model: ModelPort, package: ScenarioPackage,
                 telemetry: Telemetry | None = None) -> None:
        self.model = model
        self.package = package
        self.telemetry = telemetry or NoopTelemetry()
        self.formatter = ThinkingSafeOpenAIChatFormatter()

    async def generate(self, game_id: str, context: SceneModelContext) -> scene_schema.SceneTurn:
        system = scene_system_prompt(self.package)
        schema = scene_schema.scene_output_schema(self.package)
        prompt = await self.formatter.format(msgs=[
            Msg("system", system, "system"),
            Msg("player", serialize_scene_request(context.request), "user"),
        ])
        with self.telemetry.span(
            "single-scene-generation",
            {"game_id": game_id, "candidate_actor_ids": list(context.focus_actor_ids)},
            kind="agent", input=context.request if self.telemetry.capture_content else None,
            session_id=session_id_for_game(game_id, self.package.package_id),
        ) as span:
            response = await self.model(prompt, structured_model=schema)
            result = schema.model_validate(response.metadata)
            result = result.model_copy(update={
                "prose": result.prose.replace("\\r\\n", "\n").replace("\\n", "\n").strip(),
                "replies": [item.model_copy(update={
                    "speech": item.speech.replace("\\r\\n", "\n").replace("\\n", "\n").strip(),
                }) for item in result.replies],
            })
            span.metric("story.single_scene_model_calls", 1.0)
            if self.telemetry.capture_content:
                span.update(output=result.model_dump())
            return result.for_storage()
