# StoryLoop Harness

An interactive narrative runtime for building stories with persistent world state
and character-specific context. StoryLoop projects the player's and characters'
views into a shared scene-generation request, validates the model's proposals,
and commits events through an injected store.

The player application lives in
[StoryLoop Platform](https://github.com/storyloop0/storyloop-platform). This
library can also be used independently, without the platform's HTTP service,
database, accounts or billing.

**Status:** active incubation, Python 3.12+, MIT. APIs, configuration and saved
formats may change without backward compatibility or migration support.

## What it does

- **Context projection:** assemble role cards, recipient-visible history,
  relevant experiences and worldbook entries within a context budget.
- **Scene generation:** produce narrative, character replies and structured
  action/state proposals through AgentScope-backed model adapters.
- **Controlled commits:** validate proposals against scenario rules, commit
  causal events, and deliver observations and generated speech deterministically.
- **Replay and instrumentation:** replay saved turns, expose committed events
  and model usage, and accept an optional telemetry implementation.

```mermaid
flowchart LR
    Input[Player input] --> Context[Player and character context projections]
    Context --> Model[Structured scene generation]
    Model --> Validate[Scenario validation]
    Validate --> Store[Event and state commits]
    Store --> Output[Visible story and character observations]
```

Characters have distinct views and histories. They do not each run a ReAct loop.
The shared model sees several character contexts, so projection is not a hard
information-isolation boundary.

## Quick start: no model key required

Clone this repository and install it in a virtual environment:

```sh
git clone https://github.com/storyloop0/storyloop-harness.git
cd storyloop-harness
python -m venv .venv
# macOS / Linux:
. .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install .
python examples/offline_turn.py
```

Use a Python 3.12+ interpreter for `python`. The example uses a bundled synthetic
scenario, an in-memory store and a deterministic model. It makes no provider
requests and prints the story plus the resulting snapshot version.

### Use the turn API

Run from the repository root after installation:

```python
import asyncio
import sys

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.testing import InMemoryGameStore, OfflineModel


async def main():
    package = ScenarioPackage.load("examples/freeform")
    store = InMemoryGameStore()
    package.seed_game(store, "demo")
    engine = TurnEngine(store, package, OfflineModel())
    outcome = await engine.run_turn(
        TurnInput("demo", "Hello", "turn-1", package.version)
    )
    print(outcome.narration)
    print(outcome.snapshot.version)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
```

To connect a live model, supply a `ModelPort` implementation; the
`generation.CompatibleOpenAIChatModel` adapter uses AgentScope 2.0.9. The caller
supplies credentials and routing. To persist games, implement `GameStore` and
its expected-version commit contract.

## Integration surfaces

| Surface | Purpose |
| --- | --- |
| Top-level package | `ScenarioPackage`, `TurnEngine`, `TurnInput`, `TurnOutcome`, `GameStore`, `ModelPort`, `CampaignContext` |
| `contracts`, `ports` | Turn data and injected model/store/campaign protocols |
| `context` | `ContextBudget`, `ContextProjection`, `ProjectionReport`, `MaterialDecision`, `ContextBudgetExceeded` |
| `advanced` | Event, work and story-clock contracts for integrations |
| `generation` | Structured model transport, formatting and action suggestions |
| `telemetry`, `usage` | Tracing interfaces and scoped token usage, without pricing policy |
| `testing` | In-memory store, deterministic offline model and detached scene request inspection |

`TurnEngine.run_turn` accepts a `TurnInput` plus optional progress and campaign
time bounds. `run_ready_work` drains supported queued work, including previously
generated character speech. `TurnOutcome` contains visible story output, a
snapshot, events committed by that invocation and its model usage.

Keep request ownership, authentication, pricing and settlement in the calling
application. Internal module paths are not extension APIs.

### Inspect context and configure its budget

Run this offline example from the repository root:

```python
from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.context import ContextBudget, ContextBudgetExceeded
from storyloop_harness.testing import InMemoryGameStore, OfflineModel

package = ScenarioPackage.load("examples/freeform")  # synthetic scenario
store = InMemoryGameStore()
package.seed_game(store, "inspection")
engine = TurnEngine(store, package, OfflineModel(), context_budget=ContextBudget(
    context_window_tokens=65536, output_tokens=4096,
    safety_margin_tokens=256, max_material_tokens=16000,
))
projection = engine.project_context(
    TurnInput("inspection", "Hello", "inspect-1", package.version)
)
print(projection.report.to_dict())
```

`project_context` is synchronous and read-only. It uses the runtime's clock,
story program, responder limit and explicit player preference scope. Its detached
request contains story material; its immutable report contains source IDs, owners,
state/checkpoint versions, fields, selection reasons and estimated sizes without
source prose. Keep both developer-side. Reports are absent from player turn output
and stored story events; default telemetry records aggregate counts and estimates.

The inspected scope is history after each recipient's checkpoint, player inputs
from the last four turns, the last six player observations, visible worldbook
entries, public/system source facts and milestone candidates, and private role
material/history for selected responders. Unselected actors' private context and
other saves are outside that scope. `selected` and `truncated` are independent;
`omission_reason` records rank/item limits, field clipping (`field_limit`), or
material/window budget trimming. Source facts and milestone cues yield whole
before recent experiences; prompt omission does not change milestone eligibility.

Estimates use `utf8-conservative-v1`, a conservative character/UTF-8 heuristic,
not a provider tokenizer measurement. `scene-projection-v1` enforces the independent
serialized material cap and the complete estimated request window: system prompt,
message framing/escaping, expanded output schema, transport allowance, output
reserve and safety margin. The supported AgentScope adapter checks the actual SDK
formatted envelope before HTTP and sends one effective `max_completion_tokens`
cap. Custom `ModelPort` implementations must honor that bounded-call contract.

All public rules, source resolutions, player input, authoritative state and a
nonempty selected role identity floor are required. If they cannot fit,
`ContextBudgetExceeded` carries a content-free report and the turn fails before
model dispatch or world writes. A changed inspection request is re-estimated on
generation. This policy covers ordinary scene requests and each transport attempt;
it does not measure whole-campaign spend or provide hard isolation between roles.

`testing.project_scene_request(store, package, game_id, player_text,
context_budget=ContextBudget())` returns only a detached request dictionary using
the same projector; it also accepts `clock`, `program` and `max_responders`.

## Current boundaries

- `single_call` is the supported engine: an ordinary turn uses structured scene
  generation followed by deterministic work. Other scheduled generation tasks
  and transport retries can involve additional model requests.
- Old per-NPC ReAct execution is removed. Saves with pending `npc_reply` work
  are rejected before generation or world writes; recreate these saves.
  Current `single_npc_reply` work delivers already generated speech.
- The included store is for examples and tests. Durable persistence and
  cross-process coordination require a store and application implementation.
- Saved-turn replay and expected-version commits do not by themselves provide
  end-to-end billing recovery or distributed execution guarantees.

## Development

```sh
python -m pip install -e '.[test]' build
python -m pytest tests -q
python -m build --wheel
```

The CI workflow also installs a built wheel outside the checkout and runs the
offline example. Tests use deterministic models; they do not establish the
quality of live-model narrative output.

Further work focuses on long-session memory, reproducible quality/cost comparisons,
and stronger execution recovery.
These are development directions, not completed guarantees.

## License

[MIT](LICENSE).
