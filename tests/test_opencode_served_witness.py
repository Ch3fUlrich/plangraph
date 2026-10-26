"""D-2898/D-2900: witness served models from opencode's session store; free text is never evidence."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.spawner import resolver
from plangraph.spawner.runner import SpawnerRunner

REQUESTED = 'opencode/glm-5.3-flash-free'


def _store(tmp_path, sessions, assistants):
    """Build the store: sessions [(id, title)], assistant models [(session_id, model_id)]."""
    db = tmp_path / 'opencode' / 'opencode.db'
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)  # each run seeds the store fresh
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE session_v2 (id TEXT, title TEXT)')
        conn.execute('CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, data TEXT)')
        conn.executemany('INSERT INTO session_v2 VALUES (?, ?)', sessions)
        conn.executemany('INSERT INTO session_message VALUES (?, ?, ?, ?)',
                         [(f'm{i}', sid, 'assistant',
                           json.dumps({'model': {'id': mid, 'providerID': 'opencode'}}))
                          for i, (sid, mid) in enumerate(assistants)])


def _run_leg(tmp_path, monkeypatch, seed):
    """One fake opencode leg; seed(title) fills the store once spawn names the session."""
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)

    async def factory(*args, **kwargs):
        argv = list(args)
        seed(argv[argv.index('--title') + 1])
        async def communicate():
            return b'ok', b''
        return SimpleNamespace(pid=123, returncode=0, communicate=communicate)

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('prompt', model_or_combo=REQUESTED, client='opencode',
                                cwd=str(tmp_path),
                                extra_env={'XDG_DATA_HOME': str(tmp_path)}, wait_seconds=3)
        await runner._tasks[job.job_id]
        return job
    return asyncio.run(run())


def test_one_assistant_model_equal_to_requested_is_witnessed(tmp_path, monkeypatch):
    job = _run_leg(tmp_path, monkeypatch,
                   lambda t: _store(tmp_path, [('s1', t)], [('s1', 'glm-5.3-flash-free')]))
    assert job.served_model == 'opencode/glm-5.3-flash-free'
    assert job.served_family == 'glm'
    assert job.served_witness_reason == 'opencode session store'


def test_different_served_model_stays_unwitnessed_with_event(tmp_path, monkeypatch):
    job = _run_leg(tmp_path, monkeypatch,
                   lambda t: _store(tmp_path, [('s1', t)], [('s1', 'deepseek-v4.1-flash-free')]))
    assert job.served_model == 'unwitnessed'
    assert {'event': 'opencode_witness_unavailable'} in job.events


def test_two_models_in_one_session_stay_unwitnessed(tmp_path, monkeypatch):
    job = _run_leg(tmp_path, monkeypatch, lambda t: _store(
        tmp_path, [('s1', t)], [('s1', 'glm-5.3-flash-free'), ('s1', 'deepseek-v4.1-flash-free')]))
    assert job.served_model == 'unwitnessed'


def test_no_or_duplicate_title_row_stays_unwitnessed(tmp_path, monkeypatch):
    job = _run_leg(tmp_path, monkeypatch,
                   lambda t: _store(tmp_path, [('s1', 'plangraph-other')], [('s1', 'glm-5.3-flash-free')]))
    assert job.served_model == 'unwitnessed'
    job = _run_leg(tmp_path, monkeypatch,
                   lambda t: _store(tmp_path, [('s1', t), ('s2', t)], [('s1', 'glm-5.3-flash-free')]))
    assert job.served_model == 'unwitnessed'


def test_missing_session_db_stays_unwitnessed(tmp_path, monkeypatch):
    job = _run_leg(tmp_path, monkeypatch, lambda t: None)
    assert job.served_model == 'unwitnessed'


def test_title_flag_on_opencode_leg_and_not_on_plain_leg(tmp_path):
    async def run():
        runner = SpawnerRunner(default_cwd=str(tmp_path))
        opencode_job = await runner.spawn('p', model_or_combo=REQUESTED, client='opencode',
                                          cwd=str(tmp_path), dry_run=True)
        plain_job = await runner.spawn('p', model_or_combo=DEFAULT_LEGS[0].model, client='agy',
                                       cwd=str(tmp_path), dry_run=True)
        return opencode_job, plain_job
    opencode_job, plain_job = asyncio.run(run())
    assert opencode_job.command[-2:] == ['--title', 'plangraph-' + opencode_job.job_id]
    assert '--title' not in plain_job.command
