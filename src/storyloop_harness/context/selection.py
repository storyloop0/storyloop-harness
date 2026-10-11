"""Candidate provenance recorded before selection and clipping."""
from copy import deepcopy
from dataclasses import dataclass, replace
from .types import MaterialDecision, ContextProjection, ContextBudgetExceeded
from .budget import estimate_scene_request
from storyloop_harness.core.token_budget import estimate_tokens
from storyloop_harness.agents.scene_prompt import serialize_scene_request


def size(value):
    return estimate_tokens(serialize_scene_request(value))


@dataclass
class Candidate:
    decision: MaterialDecision
    value: object
    path: tuple
    optional: bool
    group: bool
    floor: str | None = None


class Selection:
    """Local candidate ledger; source content never enters public decisions."""
    def __init__(self):
        self.items = []

    def add(self, kind, source_id, path, raw, value=None, *, reason='required',
            selected=True, omission=None, owner=None, version=None, optional=False,
            group=False, floor=None):
        rendered = raw if value is None else value
        decision = MaterialDecision(kind, source_id, '.'.join(map(str, path)), selected,
            reason, size(raw), size(rendered) if selected else 0, owner, version,
            raw != rendered, omission or ('field_limit' if raw != rendered else None))
        self.items.append(Candidate(decision, rendered, path, optional, group, floor))
        return rendered

    def render(self, base):
        result = deepcopy(base)
        groups = {}
        for item in self.items:
            if item.group:
                groups.setdefault(item.path, [])
                if item.decision.selected:
                    groups[item.path].append(deepcopy(item.value))
            elif item.path:
                target = result
                for key in item.path[:-1]:
                    target = target[key]
                target[item.path[-1]] = deepcopy(item.value) if item.decision.selected else ''
        for path, values in groups.items():
            target = result
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = values
        return result

    def fit(self, base, package, budget, nearby, focus):
        # Source cues yield whole before recent experiences. Then discard
        # optional history/lore and summaries before reducing role elaboration.
        priority = {'source_fact': 0, 'milestone': 1, 'history': 2,
                    'worldbook_lore': 3, 'observation': 4, 'checkpoint': 5,
                    'player_input': 6, 'preferences': 7, 'role': 8}
        optional = sorted((item for item in self.items if item.optional),
                          key=lambda item: priority.get(item.decision.source_kind, 9))
        while True:
            request = self.render(base)
            report = estimate_scene_request(request, package, budget,
                decisions=tuple(item.decision for item in self.items),
                scope='ordinary-single-scene:recipient-history-after-checkpoint,visible-worldbook,public-system-source,focus-private-context')
            material_over = size(request) > budget.max_material_tokens
            if not material_over and report.total_tokens <= budget.context_window_tokens:
                return ContextProjection(request, nearby, focus, report, budget)
            reason = 'material_budget' if material_over else 'context_budget'
            item = next((item for item in optional if item.decision.selected
                         and (item.floor is None or item.value != item.floor)), None)
            if item is None:
                raise ContextBudgetExceeded(report)
            if item.floor is not None:
                item.value = item.floor
                item.decision = replace(item.decision, truncated=True,
                    estimated_tokens_after=size(item.value))
                # Clipping is separately identified from whole-item omission.
                item.decision = replace(item.decision, omission_reason=reason)
            else:
                item.decision = replace(item.decision, selected=False,
                    estimated_tokens_after=0, omission_reason=reason)
