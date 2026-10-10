"""Synthetic scene fixtures owned by the Harness test suite."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from storyloop_harness.agents.scene_turn import SceneTurn
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.world.story_blueprint import StoryBlueprint
from storyloop_harness.world.worldbook import Worldbook, WorldbookEntry
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/freeform"

class FakeGenerator:
    def __init__(self, turn: dict) -> None:
        self.turn = turn
        self.calls = 0
        self.requests = []

    async def generate(self, game_id, context):
        self.calls += 1
        self.requests.append(context.request)
        return SceneTurn.model_validate(self.turn)

def a_turn(**changes):
    result = {
        "decision": {"intent": "speech", "target_ids": ["dockhand"]},
        "prose": "你听见码头工放下缆绳。",
        "replies": [{"actor_id": "dockhand", "speech": "今天有船靠岸。"}],
        "options": [
            {"label": "询问船期", "input": "我问码头工下一班船几点到。"},
            {"label": "看看告示", "input": "我走到告示板前查看。"},
            {"label": "沿岸走走", "input": "我沿着码头走一段。"},
        ],
    }
    result.update(changes)
    return result

def source_package():
    base = ScenarioPackage.load(ROOT / "examples/freeform")
    state = deepcopy(base.initial_state)
    names = {"dockhand": "Dockhand", "vendor": "Vendor", "guard": "Guard", "guide": "Guide"}
    for actor_id in names:
        state["actors"][actor_id] = {"location": "harbor_square"}
    cards = tuple((actor_id, f"{actor_id}_card") for actor_id in names)
    book = Worldbook(base.package_id, base.version, [
        WorldbookEntry(card, f"{names[actor_id]} knows only their own harbor experiences. " * 8,
                       "actor", frozenset({actor_id}), "card")
        for actor_id, card in cards
    ])
    blueprint = StoryBlueprint.model_validate({
        "source_document": "Synthetic harbor story",
        "opening_focus": "Four workers share a public harbor scene.",
        "setup": {"player_options": [{"id": "random", "label": "Visitor", "guidance": "Visitor",
                                     "source_ref": "Source"}],
                  "tone_options": [{"id": "slow", "label": "Slow", "guidance": "Patient",
                                   "source_ref": "Source"}]},
        "facts": [{"id": "harbor", "text": "Visitors arrive by boat.",
                   "source_ref": "Source", "visibility": "public"}],
    })
    return replace(base, initial_state=state, actor_cards=cards, actor_names=names,
                   worldbook=book, story_blueprint=blueprint, initial_work=(),
                   presentation_mode="interactive")
