"""Read-only diagnostic support for inspecting a runtime scene request."""
from copy import deepcopy
from storyloop_harness.context import ContextBudget

from storyloop_harness.ports import GameStore
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.context.projection import SceneContextProjector


def project_scene_request(
    store: GameStore,
    package: ScenarioPackage,
    game_id: str,
    player_text: str,
    *,
    context_budget: ContextBudget | None = None,
    clock=None,
    program=None,
    max_responders: int = 3,
) -> dict[str, object]:
    """Return a detached projection without generation or store writes."""
    projector = SceneContextProjector(store, package, context_budget=context_budget,
                                      clock=clock, program=program, max_responders=max_responders)
    return deepcopy(projector.project(store.load(game_id), player_text).request)
