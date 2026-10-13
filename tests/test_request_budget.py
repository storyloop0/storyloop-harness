"""Budget value and projection contracts; wire behavior lives in test_request_boundary."""
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from storyloop_harness.context import ContextBudget, MaterialDecision, ProjectionReport
from storyloop_harness.context.budget import estimate_scene_request
from storyloop_harness.agents.scene_prompt import scene_system_prompt
from storyloop_harness.agents.scene_schema import scene_output_schema
from storyloop_harness.world.scenario import ScenarioPackage


@pytest.mark.parametrize('field,value', [
    ('context_window_tokens', True), ('output_tokens', 0),
    ('safety_margin_tokens', -1), ('max_material_tokens', 1.5),
])
def test_budget_rejects_non_integer_and_invalid_limits(field, value):
    with pytest.raises(ValueError):
        ContextBudget(**{field: value})


def test_budget_defaults_and_reserve_validation():
    budget = ContextBudget()
    assert (budget.context_window_tokens, budget.output_tokens,
            budget.safety_margin_tokens, budget.max_material_tokens) == (65536, 4096, 256, 16000)
    with pytest.raises(ValueError):
        ContextBudget(context_window_tokens=4352)


def test_canonical_source_and_freeform_schema_and_accounting():
    package = ScenarioPackage.load(Path(__file__).resolve().parents[1] / 'examples' / 'freeform')
    assert scene_output_schema(package).__name__ == 'NarrativeTurn'
    assert scene_system_prompt(package).endswith('输出 NarrativeTurn。')
    report = estimate_scene_request({'player_action': 'synthetic'}, package, ContextBudget())
    assert report.system_tokens > 0 and report.material_tokens > 0 and report.schema_tokens > 0
    assert report.transport_tokens >= 512
    assert report.total_tokens == sum([
        report.system_tokens, report.material_tokens, report.schema_tokens,
        report.transport_tokens, 4096, 256,
    ])
    assert report.remaining_tokens == 65536 - report.total_tokens


def test_budget_report_is_immutable_and_serializable_without_prompt_content():
    decision = MaterialDecision('history', 'event-1', 'player_history', True, 'recent', 10, 8,
                                owner='player', state_version=2, truncated=True)
    report = ProjectionReport(10, 20, 30, 40, 50, 60, 300, (decision,))
    assert report.total_tokens == 210 and report.remaining_tokens == 90
    assert json.loads(json.dumps(report.to_dict()))['decisions'][0]['source_id'] == 'event-1'
    with pytest.raises(FrozenInstanceError):
        report.output_tokens = 100
