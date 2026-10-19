"""Source-driven responders need their own projected role and history."""

from scene_fixtures import source_package

from copy import deepcopy
import json
from pathlib import Path
import unittest

from storyloop_harness.testing import InMemoryGameStore
from storyloop_harness.context.projection import SceneContextProjector
from storyloop_harness.agents.scene_schema import SceneTurn
from storyloop_harness.core.contracts import Observation, WorldEvent
from storyloop_harness.advanced import estimate_tokens
from storyloop_harness.runtime.single_call import SingleCallGameSession


ROOT = Path(__file__).resolve().parents[1]


class SceneResponderContextTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryGameStore()
        self.package = source_package()
        self.package.seed_game(self.store, "game")
        self.projector = SceneContextProjector(self.store, self.package)

    def test_four_person_source_scene_only_allows_projected_responders(self):
        context = self.projector.project(self.store.load("game"), "Hello everyone")
        self.assertEqual(len(context.nearby_actor_ids), 4)
        projected = tuple(item["id"] for item in context.request["npc_contexts"])
        self.assertEqual(context.request["candidate_responders"], list(projected))
        self.assertEqual(context.focus_actor_ids, projected)
        self.assertEqual(len(projected), 3)

    def test_explicitly_named_fourth_actor_gets_their_own_role_and_history(self):
        snapshot = self.store.load("game")
        self.store.commit("game", snapshot.version,
            WorldEvent("guide-secret", "private_fact", None, None, 0, ()),
            (Observation("guide-secret:seen", "guide-secret", "guide", "private_message",
                         "Guide privately saw the missing boat.", 0),), ())
        context = self.projector.project(self.store.load("game"), "Guide, what did you see?")
        self.assertEqual(context.focus_actor_ids[0], "guide")
        own = context.request["npc_contexts"][0]
        self.assertIn("Guide", own["role_card"])
        self.assertIn("missing boat", str(own["own_history"]))
        self.assertNotIn("missing boat", str(context.request["npc_contexts"][1:]))

    def test_unprojected_dialogue_actor_rejects_the_whole_plan(self):
        before = self.store.load("game")
        context = self.projector.project(before, "Hello everyone")
        plan = SceneTurn.model_validate({
            "decision": {"intent": "speech", "target_ids": ["dockhand", "guide"]},
            "beats": [{"kind": "dialogue", "actor_id": actor_id, "text": "Welcome."}
                        for actor_id in ("dockhand", "guide")],
            "memories": [{"actor_id": actor_id, "fact": "Greeted the visitor."}
                         for actor_id in ("dockhand", "guide")],
        })
        session = SingleCallGameSession(self.store, self.package, None, self.projector)
        with self.assertRaisesRegex(ValueError, 'authorized'):
            session._validate_plan(before, plan, context.focus_actor_ids,
                                   context.nearby_actor_ids)

    def test_empty_generated_card_falls_back_to_authored_role(self):
        before = self.store.load("game")
        before.data["actor_profiles"] = {"guide": {"role_card": "   "}}
        context = self.projector.project(before, "Guide, hello")
        self.assertTrue(context.request["npc_contexts"][0]["role_card"].strip())

    def test_source_mention_does_not_expand_responders_beyond_present_cast(self):
        before = self.store.load("game")
        before.data["actors"]["guide"]["location"] = "another_room"
        context = self.projector.project(before, "Guide, hello")
        self.assertNotIn("guide", context.focus_actor_ids)
        self.assertNotIn("guide", context.request["candidate_responders"])

    def test_budget_exhaustion_cannot_erase_all_responder_role_cards(self):
        before = self.store.load("game")
        from storyloop_harness.context import ContextBudget, ContextBudgetExceeded
        constrained = SceneContextProjector(self.store, self.package,
            context_budget=ContextBudget(context_window_tokens=2000, output_tokens=100))
        with self.assertRaises(ContextBudgetExceeded) as error:
            constrained.project(before, 'hello')
        roles = [decision for decision in error.exception.report.decisions if decision.source_kind == 'role']
        self.assertTrue(roles)
        self.assertTrue(all(decision.selected and decision.estimated_tokens_after > 0 for decision in roles))
