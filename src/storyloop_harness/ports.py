"""Ports for storage and structured model generation."""
from typing import Protocol
from storyloop_harness.core.contracts import Snapshot
from storyloop_harness.core.store_port import GameStore


class ModelResponse(Protocol):
    metadata: object
    usage: object


class ModelPort(Protocol):
    """Structured generation boundary.

    Ordinary bounded scene calls pass ``context_budget`` (ContextBudget) and
    ``max_completion_tokens`` equal to its output_tokens. Implementations must
    enforce the estimated context window before network activity, honor this
    single output cap, and never forward context_budget to the provider.
    Other generators may omit both keywords.
    """
    async def __call__(self, messages: list, *, structured_model: type, **kwargs: object) -> ModelResponse: ...


class CampaignContext(Protocol):
    """Optional narrative context supplied by a product campaign program."""
    def current_action_context(self, snapshot: "Snapshot") -> dict: ...
