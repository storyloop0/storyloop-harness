"""Build configured SDK models directly, with no Platform dependency."""
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel
from httpx2 import Timeout
from storyloop_harness.generation import CompatibleOpenAIChatModel, ThinkingSafeOpenAIChatFormatter


def create_test_model(model='test-model', key='test-key', base_url='https://example.invalid/v1',
                      *, tool_choice_policy='native', task='model'):
    return CompatibleOpenAIChatModel(
        credential=OpenAICredential(api_key=key, base_url=base_url),
        model=model, stream=False, max_retries=2,
        client_kwargs={'timeout': Timeout(90, connect=10), 'max_retries': 2},
        parameters=OpenAIChatModel.Parameters(temperature=0.7),
        formatter=ThinkingSafeOpenAIChatFormatter(),
        tool_choice_policy=tool_choice_policy, structured_output_transport='auto', task=task,
    )
