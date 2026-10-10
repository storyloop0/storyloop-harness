"""Scenario-owned status values remain authoritative across saves and turns."""

import unittest

from storyloop_harness.core.contracts import Snapshot
from storyloop_harness.world.status_fields import parse_status_fields, project_status_fields, status_effects


class StatusFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "actors": {"player": {"location": "villa"}},
            "relationships": {"a": {"met": False, "affinity": 0}},
            "player_stats": {"mood": 80},
        }
        self.declarations = [
            {"id": "mood", "label": "心情值", "path": ["player_stats", "mood"],
             "bounds": {"min": 0, "max": 100, "max_delta": 3}},
            {"id": "affinity_a", "label": "与甲的好感度",
             "path": ["relationships", "a", "affinity"],
             "when": {"path": ["relationships", "a", "met"], "equals": True},
             "bounds": {"min": 0, "max": 100, "max_delta": 2}},
            {"id": "location", "label": "当前位置", "path": ["actors", "player", "location"]},
        ]

    def test_projection_respects_visibility_and_knowledge_gate(self) -> None:
        fields = parse_status_fields(self.declarations, self.state)
        self.assertEqual(project_status_fields(fields, self.state), [
            {"id": "mood", "label": "心情值", "value": 80, "min": 0, "max": 100},
            {"id": "location", "label": "当前位置", "value": "villa"},
        ])
        self.state["relationships"]["a"]["met"] = True
        self.assertEqual(project_status_fields(fields, self.state)[1]["value"], 0)
        self.assertEqual([item["id"] for item in project_status_fields(
            fields, {"player_stats": {"mood": 80}, "actors": self.state["actors"]})],
            ["mood", "location"])

    def test_deltas_are_bounded_and_only_declared_fields_can_change(self) -> None:
        fields = parse_status_fields(self.declarations, self.state)
        snapshot = Snapshot("game", 0, 0, self.state)
        effects = status_effects(fields, snapshot, [{"id": "mood", "delta": -2}])
        self.assertEqual(effects[0].path, ("player_stats", "mood"))
        self.assertEqual(effects[0].value, 78)
        with self.assertRaisesRegex(ValueError, "delta"):
            status_effects(fields, snapshot, [{"id": "mood", "delta": 12}])
        with self.assertRaisesRegex(ValueError, "unknown"):
            status_effects(fields, snapshot, [{"id": "location", "delta": 1}])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            status_effects(fields, snapshot, [{"id": "mood", "delta": 1},
                                             {"id": "mood", "delta": 1}])


    def test_invalid_status_schema_fails_on_package_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown"):
            parse_status_fields([{"id": "x", "label": "X", "path": ["missing"]}], self.state)
        with self.assertRaisesRegex(ValueError, "bounds"):
            parse_status_fields([{"id": "location", "label": "位置",
                                  "path": ["actors", "player", "location"],
                                  "bounds": {"min": 0, "max": 100, "max_delta": 2}}], self.state)


if __name__ == "__main__":
    unittest.main()
