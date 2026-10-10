"""Read-only diagnostic support for inspecting a runtime scene request."""
from copy import deepcopy

from storyloop_harness.ports import GameStore
from storyloop_harness.world.scenario import ScenarioPackage
from storyloop_harness.agents.scene_turn import SceneContextProjector


def project_scene_request(
    store: GameStore,
    package: ScenarioPackage,
    game_id: str,
    player_text: str,
    *,
    context_window_tokens: int = 65536,
) -> dict[str, object]:
    """Return a detached projection without generation or store writes."""
    projector = SceneContextProjector(store, package, context_window_tokens=context_window_tokens)
    return deepcopy(projector.project(store.load(game_id), player_text).request)
