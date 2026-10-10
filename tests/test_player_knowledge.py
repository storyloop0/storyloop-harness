"""A prepared blueprint save reads its opening and uses one story call per turn."""

import unittest
from pathlib import Path


from storyloop_harness.runtime.player_knowledge import PlayerEncounter, accepted_encounters


ROOT = Path(__file__).resolve().parents[1]


class SourceDrivenStoryTests(unittest.TestCase):

    def test_name_on_table_does_not_identify_a_stranger(self):
        prose = "桌上的名牌写着顾云舒。一个陌生女人从楼梯下来，朝你点头。"
        encounters = accepted_encounters([
            PlayerEncounter(actor_id="female_a", evidence="名牌写着顾云舒",
                            name_learned=True),
        ], prose, {"female_a": "顾云舒"})
        self.assertEqual(encounters, [])
        seen = accepted_encounters([
            PlayerEncounter(actor_id="female_a", evidence="一个陌生女人从楼梯下来",
                            name_learned=False),
        ], prose, {"female_a": "顾云舒"})
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].name_learned)
