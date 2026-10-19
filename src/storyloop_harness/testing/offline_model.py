"""Deterministic structured model with no credentials or network calls."""
from types import SimpleNamespace
from storyloop_harness.models.usage import record_model_usage


class OfflineModel:
    async def __call__(self, messages, *, structured_model, **kwargs):
        content = structured_model.model_validate({
            'beats': [{'kind': 'narration', 'text': 'The dockhand coils a rope and waves you toward the arriving boat.'},
                      {'kind': 'dialogue', 'actor_id': 'dockhand', 'text': 'The morning boat has arrived.'}],
            'participants': ['dockhand'],
            'options': [
                {'label': 'Ask about the boat', 'input': 'When does the boat leave?'},
                {'label': 'Read the notice', 'input': 'I read the notice board.'},
                {'label': 'Walk along the dock', 'input': 'I walk along the dock.'},
            ],
        }).model_dump()
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        record_model_usage('offline', 'scene_turn', usage)
        return SimpleNamespace(metadata=content, usage=usage)
