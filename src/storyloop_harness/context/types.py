"""Public metadata-only context projection contracts."""
from dataclasses import asdict, dataclass

@dataclass(frozen=True)
class ContextBudget:
    context_window_tokens: int = 65536
    output_tokens: int = 4096
    safety_margin_tokens: int = 256
    max_material_tokens: int = 16000

    def __post_init__(self):
        for name in ('context_window_tokens', 'output_tokens', 'safety_margin_tokens', 'max_material_tokens'):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == 'safety_margin_tokens' else 1):
                raise ValueError(f'{name} must be a valid integer token limit')
        if self.output_tokens + self.safety_margin_tokens >= self.context_window_tokens:
            raise ValueError('output allowance and safety margin must fit inside context window')

@dataclass(frozen=True)
class MaterialDecision:
    source_kind: str
    source_id: str
    field: str
    selected: bool
    selection_reason: str
    estimated_tokens_before: int
    estimated_tokens_after: int
    owner: str | None = None
    state_version: int | None = None
    truncated: bool = False
    omission_reason: str | None = None

@dataclass(frozen=True)
class ProjectionReport:
    system_tokens: int
    material_tokens: int
    schema_tokens: int
    transport_tokens: int
    output_tokens: int
    safety_margin_tokens: int
    context_window_tokens: int
    decisions: tuple[MaterialDecision, ...] = ()
    scope: str = 'ordinary-single-scene'
    estimator_id: str = 'utf8-conservative-v1'
    policy_id: str = 'scene-projection-v1'

    @property
    def total_tokens(self) -> int:
        return sum((self.system_tokens, self.material_tokens, self.schema_tokens,
                    self.transport_tokens, self.output_tokens, self.safety_margin_tokens))

    @property
    def remaining_tokens(self) -> int:
        return self.context_window_tokens - self.total_tokens

    def to_dict(self) -> dict:
        return {**asdict(self), 'total_tokens': self.total_tokens, 'remaining_tokens': self.remaining_tokens}

@dataclass(frozen=True)
class ContextProjection:
    request: dict[str, object]
    nearby_actor_ids: tuple[str, ...]
    focus_actor_ids: tuple[str, ...]
    report: ProjectionReport
    budget: ContextBudget

class ContextBudgetExceeded(ValueError):
    def __init__(self, report: ProjectionReport):
        self.report = report
        super().__init__('Estimated ordinary scene request exceeds the configured context budget')
