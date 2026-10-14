"""The bundled offline demo works without an examples checkout."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

from storyloop_harness.demo import main
from storyloop_harness.storage import SQLiteGameStore


def _run(*args, cwd: Path):
    return subprocess.run(
        [sys.executable, '-m', 'storyloop_harness.demo', *map(str, args)],
        cwd=cwd, capture_output=True, text=True, encoding='utf-8',
        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=30,
    )


def _event_count(db: Path) -> int:
    with sqlite3.connect(db) as connection:
        return connection.execute('SELECT count(*) FROM events').fetchone()[0]


def _result_line(output: str, label: str) -> str:
    return next(line.removeprefix(label) for line in output.splitlines()
                if line.startswith(label))


def _narration(output: str) -> str:
    lines = output.splitlines()
    version_line = next(index for index, line in enumerate(lines)
                        if line.startswith('Snapshot version: '))
    return '\n'.join(lines[2:version_line])


def _saved_snapshot(db: Path):
    with SQLiteGameStore(db) as store:
        return store.load('installed-harbor-demo')


def test_default_uses_temporary_storage_and_bundled_resources(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    output = capsys.readouterr().out
    assert 'deterministic' in output.lower()
    assert 'Storage: temporary' in output
    assert int(_result_line(output, 'Snapshot version: ')) > 0
    assert int(_result_line(output, 'New events: ')) > 0
    assert 'Fixture model invocations: 1' in output
    assert 'Provider requests: 0' in output
    assert 'Unchanged replay: no' in output
    assert 'The dockhand coils a rope' in output
    assert list(tmp_path.iterdir()) == []


def test_persistent_replay_and_changed_input_are_process_safe(tmp_path):
    db = tmp_path / 'story.sqlite3'
    first = _run('--db', db, cwd=tmp_path)
    assert first.returncode == 0, first.stderr
    assert f'Storage: {db.resolve()}' in first.stdout
    assert 'Fixture model invocations: 1' in first.stdout
    first_narration = _narration(first.stdout)
    first_version = int(_result_line(first.stdout, 'Snapshot version: '))
    first_snapshot = _saved_snapshot(db)
    assert first_version == first_snapshot.version
    count = _event_count(db)
    replay = _run('--db', db, cwd=tmp_path)
    assert replay.returncode == 0, replay.stderr
    assert 'New events: 0' in replay.stdout
    assert 'Fixture model invocations: 0' in replay.stdout
    assert 'Provider requests: 0' in replay.stdout
    assert 'Unchanged replay: yes' in replay.stdout
    assert _narration(replay.stdout) == first_narration
    assert int(_result_line(replay.stdout, 'Snapshot version: ')) == first_version
    assert _saved_snapshot(db) == first_snapshot
    assert _event_count(db) == count
    changed = _run('--db', db, '--text', 'Different', cwd=tmp_path)
    assert changed.returncode != 0
    assert changed.stderr.startswith('Demo error:')
    assert _saved_snapshot(db) == first_snapshot
    assert _event_count(db) == count
    further = _run('--db', db, '--turn-id', 'demo-2', cwd=tmp_path)
    assert further.returncode == 0, further.stderr
    assert _event_count(db) > count


def test_missing_parent_does_not_create_database(tmp_path):
    db = tmp_path / 'missing' / 'story.sqlite3'
    result = _run('--db', db, cwd=tmp_path)
    assert result.returncode != 0
    assert 'Demo error:' in result.stderr
    assert not db.exists()


def test_default_failure_closes_and_removes_temporary_database(tmp_path, monkeypatch, capsys):
    import storyloop_harness.demo as demo

    monkeypatch.setattr(demo, 'TemporaryDirectory',
                        lambda **kwargs: TemporaryDirectory(dir=tmp_path, **kwargs))
    assert main(['--text', '']) != 0
    assert 'Demo error:' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_relative_persistent_path_resolves_against_caller(tmp_path):
    result = _run('--db', 'relative.sqlite3', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert f'Storage: {tmp_path / "relative.sqlite3"}' in result.stdout
    assert (tmp_path / 'relative.sqlite3').is_file()


def test_mismatched_saved_scenario_is_not_reset(tmp_path):
    from storyloop_harness.core.contracts import Snapshot
    from storyloop_harness.storage import SQLiteGameStore

    db = tmp_path / 'other.sqlite3'
    with SQLiteGameStore(db) as store:
        store.create_game(Snapshot('installed-harbor-demo', 0, 0, {
            'scenario': {'id': 'some-other-story', 'version': '1.0'},
        }))
    result = _run('--db', db, cwd=tmp_path)
    assert result.returncode != 0
    assert 'saved scenario identity/version' in result.stderr
    with SQLiteGameStore(db) as store:
        assert store.load('installed-harbor-demo').data['scenario']['id'] == 'some-other-story'


def test_source_only_wheel_contains_demo_resources_and_entry_point(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('pyproject.toml', 'LICENSE'):
        shutil.copy2(root / name, source / name)
    shutil.copytree(root / 'src', source / 'src',
                    ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
    build_python = os.environ.get('STORYLOOP_BUILD_PYTHON', sys.executable)
    built = subprocess.run(
        [build_python, '-m', 'build', '--wheel', '--no-isolation',
         '--outdir', str(tmp_path / 'dist'), str(source)],
        cwd=tmp_path, capture_output=True, text=True, encoding='utf-8',
        env={**os.environ, 'PYTHONUTF8': '1'}, timeout=90,
    )
    assert built.returncode == 0, built.stderr
    wheel, = (tmp_path / 'dist').glob('*.whl')
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert 'storyloop_harness/demo_scenario/manifest.json' in names
        assert 'storyloop_harness/demo_scenario/worldbook.json' in names
        entry, = (name for name in names if name.endswith('.dist-info/entry_points.txt'))
        assert archive.read(entry).decode('utf-8').strip() == (
            '[console_scripts]\nstoryloop-harness-demo = storyloop_harness.demo:main')
        assert not any(name.startswith('examples/') for name in names)
