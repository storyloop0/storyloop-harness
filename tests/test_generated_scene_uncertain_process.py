"""Independent-process crash/restart tests for guarded scene generation."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from storyloop_harness.storage import SQLiteGameStore


def child(path, count, phase, protect):
    return subprocess.run([sys.executable,
        str(Path(__file__).with_name('generated_scene_uncertain_probe.py')),
        str(path), str(count), phase, 'on' if protect else 'off'],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, 'PYTHONUTF8': '1',
             'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})


def calls(path):
    return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0


@pytest.mark.parametrize('phase,code,expected_calls', [
    ('after_marker', 48, 0), ('during_model', 49, 1), ('before_snapshot', 50, 1)])
def test_guard_blocks_fresh_process_after_durable_marker(tmp_path, phase, code, expected_calls):
    db, count = tmp_path / 'game.db', tmp_path / 'calls.txt'
    crashed = child(db, count, phase, True)
    assert crashed.returncode == code, crashed.stderr
    assert calls(count) == expected_calls
    with SQLiteGameStore(db) as store:
        before = store.load('game')
        marker = store.generated_scene_generation_marker('game', 'turn-1')
        assert marker is not None
        assert store.generated_scene_snapshot('game', 'turn-1') is None
        assert store.player_inputs_for('game') == []
        pending = store.pending_work('game')
    for operation in ('inspect', 'restart'):
        blocked = child(db, count, operation, True)
        assert blocked.returncode == 52, blocked.stderr
        assert json.loads(blocked.stdout) == {
            'uncertain': 'Generated scene outcome is uncertain', 'attempts': 0}
        assert calls(count) == expected_calls
    with SQLiteGameStore(db) as store:
        assert store.load('game') == before
        assert store.generated_scene_generation_marker('game', 'turn-1') == marker
        assert store.generated_scene_snapshot('game', 'turn-1') is None
        assert store.player_inputs_for('game') == []
        assert store.pending_work('game') == pending


def test_default_off_preserves_before_snapshot_regeneration_window(tmp_path):
    db, count = tmp_path / 'game.db', tmp_path / 'calls.txt'
    assert child(db, count, 'before_snapshot', False).returncode == 50
    assert calls(count) == 1
    checked = child(db, count, 'inspect', False)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)['inspection'] is False
    restored = child(db, count, 'restart', False)
    assert restored.returncode == 0, restored.stderr
    assert calls(count) == 2
    assert json.loads(restored.stdout)['usage_count'] > 0
    with SQLiteGameStore(db) as store:
        assert store.generated_scene_generation_marker('game', 'turn-1') is None
        assert store.generated_scene_snapshot('game', 'turn-1') is not None
        assert len(store.player_inputs_for('game')) == 1


def test_guarded_after_snapshot_exit_recovers_with_no_new_model_usage(tmp_path):
    db, count = tmp_path / 'game.db', tmp_path / 'calls.txt'
    crashed = child(db, count, 'after_snapshot', True)
    assert crashed.returncode == 51, crashed.stderr
    assert calls(count) == 1
    with SQLiteGameStore(db) as store:
        assert store.generated_scene_generation_marker('game', 'turn-1') is not None
        saved = store.generated_scene_snapshot('game', 'turn-1')
        assert saved is not None and saved.success_usage()
        assert store.load('game').version == 0
    checked = child(db, count, 'inspect', True)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)['inspection'] is True
    restored = child(db, count, 'restart', True)
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)['usage_count'] == 0
    assert calls(count) == 1
    with SQLiteGameStore(db) as store:
        assert store.generated_scene_snapshot('game', 'turn-1') == saved
        assert len(store.player_inputs_for('game')) == 1
        assert store.event_exists('game', 'turn-1:input:reply:dockhand:spoken')
