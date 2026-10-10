import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentscope.model import OpenAIChatModel
from pydantic import BaseModel

from model_helpers import create_test_model
from storyloop_harness.usage import collect_usage


class ReplySchema(BaseModel):
    text: str


class MissingUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_response_without_usage_cannot_be_collected(self) -> None:
        model = create_test_model("test-model", "test-key")
        response = SimpleNamespace(content="hello", usage=None)
        with patch.object(OpenAIChatModel, "__call__", new_callable=AsyncMock,
                          return_value=response):
            with collect_usage() as usage:
                with self.assertRaisesRegex(RuntimeError, "model did not report token usage"):
                    await model([{"role": "user", "content": "hello"}])
                self.assertEqual(usage.records, [])

    async def test_structured_response_without_usage_cannot_be_collected(self) -> None:
        model = create_test_model("test-model", "test-key")
        response = SimpleNamespace(content={"text": "hello"}, usage=None)
        with patch.object(OpenAIChatModel, "generate_structured_output", new_callable=AsyncMock,
                          return_value=response):
            with collect_usage() as usage:
                with self.assertRaisesRegex(RuntimeError, "model did not report token usage"):
                    await model([{"role": "user", "content": "hello"}],
                                structured_model=ReplySchema)
                self.assertEqual(usage.records, [])

    async def test_missing_usage_is_allowed_without_collection(self) -> None:
        model = create_test_model("test-model", "test-key")
        response = SimpleNamespace(content="hello", usage=None)
        with patch.object(OpenAIChatModel, "__call__", new_callable=AsyncMock,
                          return_value=response):
            self.assertIs(await model([{"role": "user", "content": "hello"}]), response)
        structured = SimpleNamespace(content={"text": "hello"}, usage=None)
        with patch.object(OpenAIChatModel, "generate_structured_output", new_callable=AsyncMock,
                          return_value=structured):
            result = await model([{"role": "user", "content": "hello"}],
                                 structured_model=ReplySchema)
            self.assertEqual(result.metadata, structured.content)


class ToolChoiceCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_only_policy_converts_forced_tool_choice(self) -> None:
        model = create_test_model(
            "qwen3.8-max", "test-token", "https://example.invalid/v1",
            tool_choice_policy="auto_only",
        )
        with patch.object(OpenAIChatModel, "__call__", new_callable=AsyncMock) as call:
            await model([{"role": "user", "content": "hello"}], tools=[{"type": "function"}], tool_choice="required")

        self.assertEqual(call.await_args.kwargs["tool_choice"].mode, "auto")

    async def test_native_policy_preserves_forced_tool_choice(self) -> None:
        model = create_test_model(
            "other-model", "test-token", "https://example.invalid/v1",
            tool_choice_policy="native",
        )
        with patch.object(OpenAIChatModel, "__call__", new_callable=AsyncMock) as call:
            await model([{"role": "user", "content": "hello"}], tools=[{"type": "function"}], tool_choice="required")

        self.assertEqual(call.await_args.kwargs["tool_choice"].mode, "required")


if __name__ == "__main__":
    unittest.main()
