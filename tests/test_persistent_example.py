"""The installed public API example survives separate interpreter processes."""
import json
import asyncio
import os
from pathlib import Path
import runpy
import subprocess
import sys


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'persistent_story.py'


def run_example(db, turn_id, text='打破橱窗', *extra):
    result = subprocess.run(
        [sys.executable, str(EXAMPLE), '--db', str(db), '--turn-id', turn_id,
         '--text', text, *extra], capture_output=True, encoding='utf-8',
        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_example_commits_reopens_replays_and_continues(tmp_path):
    db = tmp_path / 'story.sqlite3'
    first = run_example(db, 'break-1')
    assert first['window_broken'] is True
    assert first['model_calls'] == 1
    replay = run_example(db, 'break-1')
    assert replay['version'] == first['version']
    assert replay['narration'] == first['narration']
    assert replay['model_calls'] == 0
    repeat = run_example(db, 'break-2')
    assert repeat['window_broken'] is True
    assert repeat['version'] > first['version']
    assert repeat['narration'] != first['narration']
    assert repeat['model_calls'] == 1


def test_example_live_mode_requires_credentials_before_creating_save(tmp_path):
    db = tmp_path / 'unused.sqlite3'
    environment = {**os.environ, 'STORY_MODEL_API_KEY': '', 'PYTHONIOENCODING': 'utf-8'}
    result = subprocess.run([sys.executable, str(EXAMPLE), '--db', str(db), '--live',
                             '--turn-id', 'live-1', '--text', 'Hello'],
                            capture_output=True, encoding='utf-8', env=environment, timeout=30)
    assert result.returncode != 0
    assert 'STORY_MODEL_API_KEY' in result.stderr
    assert not db.exists()


def test_live_model_wiring_constructs_and_closes_without_requests(monkeypatch):
    monkeypatch.setenv('STORY_MODEL_API_KEY', 'offline-constructor-test')
    monkeypatch.setenv('STORY_MODEL_NAME', 'example-model')
    monkeypatch.setenv('STORY_MODEL_BASE_URL', 'https://example.invalid/v1')
    namespace = runpy.run_path(str(EXAMPLE))
    model = namespace['live_model']()
    try:
        assert model.model == 'example-model'
        assert model.stream is False
        assert str(model.client.base_url) == 'https://example.invalid/v1/'
    finally:
        asyncio.run(model.client.close())
