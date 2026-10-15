"""Private reported-token coverage for one execution and its send attempts."""
from dataclasses import dataclass

from .usage import ModelUsage


@dataclass
class ReportedTokenLedger:
    known_tokens: int = 0
    complete: bool = True


@dataclass
class AttemptTokenUsage:
    ledger: ReportedTokenLedger
    sends: int = 0
    last_valid_usage: ModelUsage | None = None
    settled: bool = False

    def settle(self, *, complete: bool) -> None:
        if self.settled:
            return
        self.settled = True
        if self.sends == 0:
            return
        if self.last_valid_usage is not None:
            self.ledger.known_tokens += (
                self.last_valid_usage.input_tokens + self.last_valid_usage.output_tokens
            )
        self.ledger.complete = self.ledger.complete and (
            self.sends == 1 and self.last_valid_usage is not None and complete
        )
