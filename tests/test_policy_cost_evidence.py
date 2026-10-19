"""Cost evidence through the actual policy runner, SDK adapter, and CLI."""
import asyncio
import importlib
import json

import httpx2
import pytest

from model_helpers import create_test_model
from examples.policy_comparison.pricing import PriceSnapshot, load_pricing
from examples.policy_comparison.runner import run_local
from storyloop_harness.generation import GenerationBudget


def _data(models=None):
    return {"schema_version": 1, "pricing_id": "SYNTHETIC_RATE_ONLY",
            "as_of": "2026-10-18", "currency": "USD", "models": models if models is not None else {
                "offline-test": {"input_per_million": "1", "output_per_million": "2",
                                 "cached_input_per_million": "0.5"}}}


def _price(models=None):
    return PriceSnapshot(_data(models))


def _completion(arguments, usage):
    return {"id": "offline", "created": 0, "model": "offline-test",
            "object": "chat.completion", "choices": [{"index": 0,
                "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "tool", "type": "function", "function": {
                    "name": "generate_structured_output", "arguments": arguments}}]}}],
            "usage": usage}


def _factory(transport):
    def factory():
        return create_test_model(model="offline-test", key="CREDENTIAL_SENTINEL",
                                 base_url="https://URL_SENTINEL.invalid/v1",
                                 http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
                                 sdk_retries=2, task="scene_turn",
                                 generation_budget=GenerationBudget(3, 90))
    return factory


def _row(transport, *, pricing=None, output_path=None):
    report = run_local(case_ids=("old-clue",), policy_ids=("history-recent-v1",),
                       model_mode="sdk_mock", model_factory=_factory(transport),
                       pricing=pricing, output_path=output_path)
    return report, report["rows"][0]


def _reply(text="window breaks"):
    return json.dumps({"beats": [{"kind": "narration", "text": text}], "interaction": "action",
                       "action_id": "break_shop_window", "action": {"status": "occurred"}})


def test_native_sdk_single_attempt_price_is_frozen(tmp_path):
    wire = []
    source = _data()
    file = tmp_path / "pricing.json"
    file.write_text(json.dumps(source), encoding="utf-8")
    price = load_pricing(file)
    original_metadata = price.to_metadata()
    source["models"]["offline-test"]["input_per_million"] = "999"

    def transport(request):
        wire.append(request)
        n = len(wire)
        if n == 1:
            file.write_text(json.dumps(source), encoding="utf-8")
        arguments = _reply()
        return httpx2.Response(200, json=_completion(arguments, {
            "prompt_tokens": 10 * n, "completion_tokens": n,
            "total_tokens": 11 * n}))

    report, row = _row(transport, pricing=price)
    assert row["passed"] is True
    assert len(wire) == len(row["attempt_summary"]["attempts"]) == 1
    assert row["known_cost"] == row["cost"] == "0.000012"
    assert row["cost_status"] == "estimated"
    assert row["cost_reasons"] == row["unpriced_models"] == []
    assert row["cost_currency"] == "USD"
    assert json.loads(file.read_text(encoding='utf-8')) == source
    assert row["metadata"]["pricing"] == original_metadata
    assert row["metadata"]["pricing"]["snapshot"]["models"]["offline-test"]["input_per_million"] == "1"
    assert row["replay_attempt_count"] == row["replay"]["new_calls"] == row["replay"]["usage_count"] == 0
    assert not any(secret in json.dumps(report) for secret in
                   ("CREDENTIAL_SENTINEL", "URL_SENTINEL", "window breaks"))


def test_native_sdk_cache_and_all_failed_complete_usage_are_priced():
    def cache_transport(request):
        return httpx2.Response(200, json=_completion(_reply(), {
            "prompt_tokens": 30, "completion_tokens": 3, "total_tokens": 33,
            "prompt_tokens_details": {"cached_tokens": 10}}))
    _, cached = _row(cache_transport, pricing=_price())
    assert cached["known_cost"] == cached["cost"] == "0.000031"

    def failed_transport(request):
        return httpx2.Response(200, json=_completion("invalid-json", {
            "prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}))
    _, failed = _row(failed_transport, pricing=_price())
    assert failed["passed"] is False
    assert failed["attempt_summary"]["usage_complete"] is True
    assert failed["known_cost"] == failed["cost"]
    assert failed["cost_status"] == "estimated"
    assert failed["cost_reasons"] == []


def test_native_sdk_missing_usage_and_unpriced_model_remain_unknown():
    def transport(request):
        return httpx2.Response(200, json=_completion("invalid-json", None))
    _, missing = _row(transport, pricing=_price())
    assert missing["known_cost"] is missing["cost"] is None
    assert missing["cost_reasons"] == ["missing_usage"]
    assert missing["cost_status"] == "unknown"

    def complete(request):
        return httpx2.Response(200, json=_completion(_reply(), {
            "prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}))
    _, unpriced = _row(complete, pricing=_price({}))
    assert unpriced["passed"] is True
    assert unpriced["cost"] is unpriced["known_cost"] is None
    assert unpriced["cost_reasons"] == ["unpriced_model"]
    assert unpriced["unpriced_models"] == ["offline-test"]


def test_no_pricing_and_fixture_with_pricing_remain_unknown():
    report = run_local(pricing=_price())
    assert report["passed"] is True and len(report["rows"]) == 12
    assert all(item["cost"] is item["known_cost"] is None and
               item["cost_reasons"] == ["no_attempts"] for item in report["rows"])
    row = report["rows"][0]
    assert row["passed"] is True
    assert row["known_cost"] is row["cost"] is None
    assert row["cost_reasons"] == ["no_attempts"]
    assert row["metadata"]["pricing"] == _price().to_metadata()
    row["metadata"]["pricing"]["snapshot"]["models"].clear()
    assert report["rows"][1]["metadata"]["pricing"] == _price().to_metadata()
    without = run_local(case_ids=("old-clue",), policy_ids=("history-recent-v1",))["rows"][0]
    assert without["cost_status"] == "unknown: no pricing supplied"
    assert without["cost_reasons"] == ["no_pricing"]
    assert without["metadata"]["pricing"] is None


def test_preparation_failure_has_unknown_cost_and_frozen_pricing():
    calls = []
    def factory():
        calls.append(1)
        raise RuntimeError("EXCEPTION_SENTINEL")
    report = run_local(case_ids=("old-clue",), policy_ids=("history-recent-v1",),
                       model_mode="sdk_mock", model_factory=factory, pricing=_price())
    row = report["rows"][0]
    assert calls == [1]
    assert row["error"] == {"type": "RuntimeError"}
    assert row["cost_reasons"] == ["no_attempts"]
    assert row["metadata"]["pricing"] == _price().to_metadata()
    assert "EXCEPTION_SENTINEL" not in json.dumps(report)


def test_first_summary_failure_keeps_complete_unknown_cost_evidence(monkeypatch):
    from examples.policy_comparison.cases import CASES
    from examples.policy_comparison.task import task
    from storyloop_harness.context import ContextPolicy

    subject = importlib.import_module("examples.policy_comparison.task")
    original = RuntimeError("EXCEPTION_SENTINEL")
    summaries = []
    models = []
    captured = []
    price = _price()

    def fail_summary(records):
        summaries.append(records)
        raise original

    monkeypatch.setattr(subject, "summarize_attempt_usage", fail_summary)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(task(case=CASES[0], policy=ContextPolicy.DEFAULT,
                         model_mode="sdk_mock", model_factory=lambda: models.append(1),
                         evidence_sink=captured.append, pricing=price))
    assert caught.value is original
    assert summaries == [()]
    assert models == []
    assert len(captured) == 1
    row = captured[0]
    assert row["passed"] is False
    assert row["error"] == {"type": "RuntimeError"}
    assert row["timing_status"]["preparation"] == "failed"
    assert {key: row[key] for key in
            ("cost", "known_cost", "cost_currency", "cost_status", "cost_reasons",
             "unpriced_models")} == {
                 "cost": None, "known_cost": None, "cost_currency": "USD",
                 "cost_status": "unknown", "cost_reasons": ["no_attempts"],
                 "unpriced_models": []}
    assert row["metadata"]["pricing"] == price.to_metadata()
    assert "EXCEPTION_SENTINEL" not in json.dumps(row)


def test_invalid_sdk_usage_does_not_create_a_zero_cost():
    def transport(request):
        return httpx2.Response(200, json=_completion(_reply(), {
            "prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1}))
    _, row = _row(transport, pricing=_price())
    assert row["attempt_summary"]["invalid_attempts"] == 1
    assert row["known_cost"] is row["cost"] is None
    assert row["cost_reasons"] == ["invalid_usage"]


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_replay_failure_or_cancellation_retains_first_cost_and_original_error(
        tmp_path, monkeypatch, error_type):
    subject = importlib.import_module("examples.policy_comparison.task")
    actual_engine = subject.TurnEngine
    engines = []
    original = error_type("EXCEPTION_SENTINEL")
    class Engine(actual_engine):
        def __init__(self, *args, **kwargs):
            engines.append(self)
            super().__init__(*args, **kwargs)

        async def run_turn(self, turn):
            if len(engines) == 2 and self is engines[1]:
                raise original
            return await super().run_turn(turn)
    monkeypatch.setattr(subject, "TurnEngine", Engine)
    calls = []
    def transport(request):
        calls.append(1)
        return httpx2.Response(200, json=_completion(_reply(), {
            "prompt_tokens": 30, "completion_tokens": 3, "total_tokens": 33}))
    output = tmp_path / "partial.json"
    if error_type is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError) as caught:
            _row(transport, pricing=_price(), output_path=output)
        assert caught.value is original
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["status"] == "cancelled"
    else:
        report, _ = _row(transport, pricing=_price(), output_path=output)
        assert report["passed"] is False
    row = report["rows"][0]
    assert calls == [1]
    assert row["known_cost"] == row["cost"] == "0.000036"
    assert row["attempt_summary"]["usage_complete"] is True
    assert row["error"] == {"type": error_type.__name__}
    assert "EXCEPTION_SENTINEL" not in json.dumps(report)


def test_incomplete_sdk_stream_preserves_known_subtotal_on_cancellation():
    from examples.policy_comparison.cases import CASES
    from examples.policy_comparison.task import task
    from storyloop_harness.context import ContextPolicy
    entered = asyncio.Event()
    blocker = asyncio.Event()
    captured = []

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            payload = {"id": "offline", "created": 0, "model": "offline-test",
                       "object": "chat.completion.chunk", "choices": [],
                       "usage": {"prompt_tokens": 7, "completion_tokens": 2,
                                 "total_tokens": 9}}
            yield ("data: " + json.dumps(payload) + "\n\n").encode()
            entered.set()
            await blocker.wait()

        async def aclose(self):
            pass

    def factory():
        model = _factory(lambda request: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()))()
        model.stream = True
        return model

    async def run():
        running = asyncio.create_task(task(case=CASES[0], policy=ContextPolicy.DEFAULT,
                                           model_mode="sdk_mock", model_factory=factory,
                                           evidence_sink=captured.append, pricing=_price()))
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    asyncio.run(run())
    assert len(captured) == 1
    row = captured[0]
    assert row["provider_input_tokens"] == 7
    assert row["provider_output_tokens"] == 2
    assert row["known_cost"] == "0.000011"
    assert row["cost"] is None
    assert row["cost_reasons"] == ["incomplete_stream"]
    assert row["error"] == {"type": "CancelledError"}


def test_runner_minimal_failure_has_unknown_cost_and_pricing():
    async def broken(**kwargs):
        raise RuntimeError("EXCEPTION_SENTINEL")
    report = run_local(task_fn=broken, case_ids=("old-clue",),
                       policy_ids=("history-recent-v1",), pricing=_price())
    row = report["rows"][0]
    assert row["known_cost"] is row["cost"] is None
    assert row["cost_reasons"] == ["no_attempts"]
    assert row["metadata"]["pricing"] == _price().to_metadata()
    assert "EXCEPTION_SENTINEL" not in json.dumps(report)


def test_cli_loads_snapshot_and_rejects_bad_file_before_runner(tmp_path, monkeypatch, capsys):
    from examples.policy_comparison import __main__
    file = tmp_path / "SENSITIVE_PATH.json"
    file.write_text(json.dumps(_data()), encoding="utf-8")
    assert __main__.main(["--pricing", str(file), "--case", "old-clue",
                          "--policy", "history-recent-v1"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["metadata"]["pricing"] == _price().to_metadata()
    assert row["cost"] is None and row["cost_reasons"] == ["no_attempts"]
    calls = []
    monkeypatch.setattr(__main__, "run_local", lambda **kwargs: calls.append(kwargs))
    for bad in ('{"pricing_id": "x", "pricing_id": "y"}', '{', None):
        if bad is None:
            file.unlink()
        else:
            file.write_text(bad, encoding="utf-8")
        with pytest.raises(SystemExit):
            __main__.main(["--pricing", str(file)])
        err = capsys.readouterr().err
        assert "invalid pricing file" in err
        assert "SENSITIVE_PATH" not in err
    assert calls == []


@pytest.mark.parametrize("field", ["pricing_id", "model"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high", "low"])
def test_cli_rejects_escaped_lone_surrogate_before_runner(
        tmp_path, monkeypatch, capsys, field, surrogate):
    from examples.policy_comparison import __main__

    data = _data()
    if field == "pricing_id":
        data["pricing_id"] = "synthetic" + surrogate
    else:
        data["models"] = {"offline-test" + surrogate: data["models"]["offline-test"]}
    file = tmp_path / "SENSITIVE_PATH.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    calls = []
    monkeypatch.setattr(__main__, "run_local", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(SystemExit):
        __main__.main(["--pricing", str(file)])
    err = capsys.readouterr().err
    assert "invalid pricing file" in err
    assert "SENSITIVE_PATH" not in err
    assert calls == []


def test_task_and_runner_reject_unvalidated_pricing_before_model():
    from examples.policy_comparison.cases import CASES
    from examples.policy_comparison.task import task
    from storyloop_harness.context import ContextPolicy
    calls = []
    def factory():
        calls.append(1)
        raise AssertionError("constructed")
    with pytest.raises(ValueError, match="^invalid pricing snapshot$"):
        run_local(case_ids=("old-clue",), policy_ids=("history-recent-v1",),
                  model_mode="sdk_mock", model_factory=factory, pricing=_data())
    with pytest.raises(ValueError, match="^invalid pricing snapshot$"):
        asyncio.run(task(case=CASES[0], policy=ContextPolicy.DEFAULT,
                         model_mode="sdk_mock", model_factory=factory, pricing=_data()))
    assert calls == []
