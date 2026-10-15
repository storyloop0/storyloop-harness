"""Reported token policy and final-send admission without SDK involvement."""
from dataclasses import FrozenInstanceError
from time import monotonic

import pytest

from storyloop_harness.generation import (
    ExecutionBudget, ExecutionRequestLimitExceeded, ExecutionTimeout,
    ExecutionTokenLimitExceeded, ExecutionUsageUnavailable,
    GenerationBudget, GenerationRequestLimitExceeded, GenerationTimeout,
)
from storyloop_harness.models.execution_control import ExecutionControl, admit_send
from storyloop_harness.models.execution_tokens import AttemptTokenUsage, ReportedTokenLedger
from storyloop_harness.models.generation_control import GenerationControl
from storyloop_harness.models.usage import ModelUsage


@pytest.mark.parametrize('value', [True, False, 0, -1, 1.5, '10'])
def test_reject_invalid_token_limit(value):
    with pytest.raises(ValueError):
        ExecutionBudget(max_reported_tokens=value)


def test_optional_frozen_positionally_compatible():
    assert ExecutionBudget(6, 180).max_reported_tokens is None
    assert ExecutionBudget(6, 180, 1).max_reported_tokens == 1
    with pytest.raises(FrozenInstanceError):
        ExecutionBudget().max_reported_tokens = 1


@pytest.mark.parametrize('sends,valid,complete,total,covered', [
    (0, False, False, 0, True), (1, False, False, 0, False),
    (1, True, True, 12, True), (1, True, False, 12, False),
    (2, True, True, 12, False),
])
def test_settlement(sends, valid, complete, total, covered):
    ledger = ReportedTokenLedger()
    receipt = AttemptTokenUsage(ledger, sends=sends)
    if valid:
        receipt.last_valid_usage = ModelUsage('m', 't', 10, 2, 8)
    receipt.settle(complete=complete)
    receipt.settle(complete=complete)
    assert (ledger.known_tokens, ledger.complete) == (total, covered)


def test_valid_zero_usage_and_multiple_receipts():
    ledger = ReportedTokenLedger()
    first = AttemptTokenUsage(ledger, sends=1, last_valid_usage=ModelUsage('m', 't', 0, 0))
    second = AttemptTokenUsage(ledger, sends=1, last_valid_usage=ModelUsage('m', 't', 4, 6, 3))
    first.settle(complete=True)
    second.settle(complete=True)
    assert ledger.known_tokens == 10
    assert ledger.complete


def test_incomplete_coverage_is_sticky():
    ledger = ReportedTokenLedger()
    AttemptTokenUsage(ledger, sends=1).settle(complete=False)
    AttemptTokenUsage(ledger, sends=1, last_valid_usage=ModelUsage('m', 't', 2, 3)).settle(complete=True)
    assert ledger.known_tokens == 5
    assert not ledger.complete


def test_unknown_precedes_limit_and_no_extra_claim():
    control = ExecutionControl(ExecutionBudget(6, 180, 12), monotonic() + 180, None)
    receipt = AttemptTokenUsage(control.token_usage)
    admit_send(control, None, receipt)
    receipt.last_valid_usage = ModelUsage('m', 't', 10, 2)
    receipt.settle(complete=True)
    with pytest.raises(ExecutionTokenLimitExceeded, match='^Turn execution reported token limit reached$'):
        admit_send(control, None, AttemptTokenUsage(control.token_usage))
    control.token_usage.complete = False
    with pytest.raises(ExecutionUsageUnavailable, match='^Turn execution token usage unavailable$'):
        admit_send(control, None, AttemptTokenUsage(control.token_usage))
    assert control.sends == 1


@pytest.mark.parametrize('observed', [False, True])
def test_disabled_policy_keeps_admission(observed):
    control = ExecutionControl(ExecutionBudget(), monotonic() + 180, None)
    control.token_usage.complete = False
    receipt = AttemptTokenUsage(control.token_usage) if observed else None
    admit_send(control, None, receipt)
    assert control.sends == 1
    if receipt is not None:
        assert receipt.sends == 1


@pytest.mark.parametrize('kind', ['none', 'foreign', 'settled'])
def test_unusable_receipt_rejected_without_claim(kind):
    execution = ExecutionControl(ExecutionBudget(max_reported_tokens=1), monotonic() + 180, None)
    generation = GenerationControl(GenerationBudget(), monotonic() + 180, object(), None)
    receipt = {'none': None, 'foreign': AttemptTokenUsage(ReportedTokenLedger()),
               'settled': AttemptTokenUsage(execution.token_usage, settled=True)}[kind]
    with pytest.raises(ExecutionUsageUnavailable):
        admit_send(execution, generation, receipt)
    assert (execution.sends, generation.sends) == (0, 0)
    if receipt is not None:
        assert receipt.sends == 0


def test_rejected_limit_does_not_claim_generation_or_receipt():
    execution = ExecutionControl(ExecutionBudget(max_reported_tokens=1), monotonic() + 180, None)
    generation = GenerationControl(GenerationBudget(), monotonic() + 180, object(), None)
    execution.token_usage.known_tokens = 1
    receipt = AttemptTokenUsage(execution.token_usage)
    with pytest.raises(ExecutionTokenLimitExceeded):
        admit_send(execution, generation, receipt)
    assert (execution.sends, generation.sends, receipt.sends) == (0, 0, 0)


@pytest.mark.parametrize('limited', ['execution', 'generation'])
def test_request_limit_precedes_token_checks(limited):
    execution = ExecutionControl(ExecutionBudget(1, 180, 1), monotonic() + 180, None)
    generation = GenerationControl(GenerationBudget(1, 180), monotonic() + 180, object(), None)
    execution.token_usage.complete = False
    if limited == 'execution':
        execution.sends = 1
        expected = ExecutionRequestLimitExceeded
    else:
        generation.sends = 1
        expected = GenerationRequestLimitExceeded
    with pytest.raises(expected):
        admit_send(execution, generation)
    assert (execution.sends, generation.sends) == ((1, 0) if limited == 'execution' else (0, 1))


@pytest.mark.parametrize('earlier', ['execution', 'generation'])
def test_earlier_expired_deadline_precedes_token_checks(earlier):
    now = monotonic()
    execution = ExecutionControl(ExecutionBudget(max_reported_tokens=1),
                                 now - (2 if earlier == 'execution' else 1), None)
    generation = GenerationControl(GenerationBudget(),
                                   now - (1 if earlier == 'execution' else 2), object(), None)
    execution.token_usage.complete = False
    with pytest.raises(ExecutionTimeout if earlier == 'execution' else GenerationTimeout):
        admit_send(execution, generation)
    assert (execution.sends, generation.sends) == (0, 0)
