"""D-2898/D-2900: witness served models from opencode's session store; free text is never evidence."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.review.verdict import parse_test_gaming
from plangraph.spawner import resolver
from plangraph.spawner.runner import SpawnerRunner, _opencode_served_model

REQUESTED = 'opencode/glm-5.3-flash-free'


def _store(home, sessions, assistants):
    """Build the store: sessions [(id, title)], assistant models [(session_id, model_id)]."""
    db = home / 'opencode' / 'opencode.db'
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


def _run_leg(tmp_path, monkeypatch, seed, role='review', exit_code=0, extra_env=None):
    """One fake opencode leg; seed(title, data_home) fills the store once spawn names the session."""
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)

    ctx = {}

    async def factory(*args, **kwargs):
        argv = list(args)
        ctx['env'] = dict(kwargs['env'])
        data_home = Path(kwargs['env'].get('XDG_DATA_HOME') or '')
        if data_home.exists():
            ctx['mode'] = data_home.stat().st_mode & 0o777
        seed(argv[argv.index('--title') + 1], data_home)
        async def communicate():
            return b'ok', b''
        return SimpleNamespace(pid=123, returncode=exit_code, communicate=communicate)

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('prompt', model_or_combo=REQUESTED, role=role, client='opencode',
                                cwd=str(tmp_path),
                                extra_env={'XDG_DATA_HOME': str(tmp_path), **(extra_env or {})},
                                wait_seconds=3)
        await runner._tasks[job.job_id]
        return job
    return asyncio.run(run()), ctx


def test_one_assistant_model_equal_to_requested_is_witnessed(tmp_path, monkeypatch):
    job, _ = _run_leg(tmp_path, monkeypatch,
                      lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]))
    # D-2905 (2): the store is request-side -- never 'served' evidence.
    assert job.request_side_model == 'opencode/glm-5.3-flash-free'
    assert job.witness_kind == 'request-side-opencode-store'
    assert job.served_model == 'unwitnessed'
    assert job.served_family == 'unwitnessed'
    assert job.served_witness_reason != 'opencode session store'
    assert {'event': 'opencode_tools_denied'} in job.events


def test_different_served_model_stays_unwitnessed_with_event(tmp_path, monkeypatch):
    job, _ = _run_leg(tmp_path, monkeypatch,
                      lambda t, home: _store(home, [('s1', t)], [('s1', 'deepseek-v4.1-flash-free')]))
    assert job.served_model == 'unwitnessed'
    assert job.request_side_model is None
    assert job.witness_kind is None
    assert {'event': 'opencode_witness_unavailable'} in job.events


def test_two_models_in_one_session_stay_unwitnessed(tmp_path, monkeypatch):
    job, _ = _run_leg(tmp_path, monkeypatch, lambda t, home: _store(
        home, [('s1', t)], [('s1', 'glm-5.3-flash-free'), ('s1', 'deepseek-v4.1-flash-free')]))
    assert job.served_model == 'unwitnessed'


def test_no_or_duplicate_title_row_stays_unwitnessed(tmp_path, monkeypatch):
    job, _ = _run_leg(tmp_path, monkeypatch,
                      lambda t, home: _store(home, [('s1', 'plangraph-other')], [('s1', 'glm-5.3-flash-free')]))
    assert job.served_model == 'unwitnessed'
    job, _ = _run_leg(tmp_path, monkeypatch,
                      lambda t, home: _store(home, [('s1', t), ('s2', t)], [('s1', 'glm-5.3-flash-free')]))
    assert job.served_model == 'unwitnessed'


def test_missing_session_db_stays_unwitnessed(tmp_path, monkeypatch):
    job, _ = _run_leg(tmp_path, monkeypatch, lambda t, home: None)
    assert job.served_model == 'unwitnessed'


def test_review_leg_store_dir_is_private_and_removed_after_run(tmp_path, monkeypatch):
    job, ctx = _run_leg(tmp_path, monkeypatch,
                        lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]))
    home = Path(ctx['env']['XDG_DATA_HOME'])
    assert home != tmp_path          # never the shared, child-writable store
    assert ctx['mode'] == 0o700      # spawner-private before launch
    assert not home.exists()         # removed once the run ended


def test_non_review_leg_reads_no_store_and_keeps_caller_xdg(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('plangraph.spawner.runner._opencode_served_model',
                        lambda *a, **k: calls.append(a) or None)
    job, ctx = _run_leg(tmp_path, monkeypatch,
                        lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]),
                        role='implement')
    assert Path(ctx['env']['XDG_DATA_HOME']) == tmp_path
    assert calls == []
    assert job.served_model == 'unwitnessed'


def test_opencode_tools_denied_requires_every_tool_deny():
    from plangraph.spawner.runner import _DENIED_TOOLS, _opencode_tools_denied
    assert _opencode_tools_denied(json.dumps({'permission': {t: 'deny' for t in _DENIED_TOOLS}}))
    lax = {t: 'deny' for t in _DENIED_TOOLS}
    lax['edit'] = 'allow'
    assert not _opencode_tools_denied(json.dumps({'permission': lax}))
    assert not _opencode_tools_denied(json.dumps({'permission': {'edit': 'deny'}}))
    assert not _opencode_tools_denied('not json')
    assert not _opencode_tools_denied('')


def test_tools_not_denied_yields_no_request_side_model_and_event(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('plangraph.spawner.runner._opencode_served_model',
                        lambda *a, **k: calls.append(a) or None)
    job, _ = _run_leg(tmp_path, monkeypatch,
                      lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]),
                      extra_env={'OPENCODE_CONFIG_CONTENT': 'not json'})
    assert job.served_model == 'unwitnessed'
    assert {'event': 'opencode_tools_not_denied'} in job.events
    assert job.request_side_model is None
    assert calls == []   # the store is never read when tools are not denied


def test_nonzero_exit_leaves_no_request_side_model(tmp_path, monkeypatch):
    job, _ = _run_leg(tmp_path, monkeypatch,
                      lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]),
                      exit_code=3)
    assert job.request_side_model is None
    assert job.witness_kind is None
    assert job.served_model == 'unwitnessed'


def test_malformed_or_keyless_store_rows_yield_none(tmp_path):
    db = tmp_path / 'opencode' / 'opencode.db'

    def seed_raw(row):
        db.parent.mkdir(parents=True, exist_ok=True)
        db.unlink(missing_ok=True)
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE session_v2 (id TEXT, title TEXT)')
            conn.execute('CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, data TEXT)')
            conn.execute("INSERT INTO session_v2 VALUES ('s1', 't')")
            conn.execute("INSERT INTO session_message VALUES ('m1', 's1', 'assistant', ?)", (row,))

    seed_raw('not json')
    assert _opencode_served_model(db, 't') is None
    seed_raw(json.dumps({'no_model': 1}))
    assert _opencode_served_model(db, 't') is None


def test_session_store_db_opened_read_only(tmp_path, monkeypatch):
    _store(tmp_path, [('s1', 't')], [('s1', 'glm-5.3-flash-free')])
    db = tmp_path / 'opencode' / 'opencode.db'
    captured = {}
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        captured['uri'] = database
        captured['conn'] = real_connect(database, *args, **kwargs)
        return captured['conn']

    monkeypatch.setattr('plangraph.spawner.runner.sqlite3.connect', spy)
    assert _opencode_served_model(db, 't') == 'opencode/glm-5.3-flash-free'
    assert captured['uri'].startswith('file:') and captured['uri'].endswith('?mode=ro')
    before = db.read_bytes()
    with pytest.raises(sqlite3.Error):
        captured['conn'].execute("INSERT INTO session_v2 VALUES ('sX', 'x')")
    assert db.read_bytes() == before
    ro = real_connect(captured['uri'], uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO session_v2 VALUES ('sY', 'y')")


def test_db_path_with_space_and_reserved_chars_opens(tmp_path):
    home = tmp_path / 'we ird?dir#x'
    _store(home, [('s1', 't')], [('s1', 'glm-5.3-flash-free')])
    assert _opencode_served_model(home / 'opencode' / 'opencode.db', 't') == 'opencode/glm-5.3-flash-free'


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


def test_title_flag_before_any_prompt_positional(tmp_path, monkeypatch):
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    monkeypatch.setattr('plangraph.spawner.runner.build_command',
                        lambda **kw: ['opencode', 'run', '--standalone', '--model', REQUESTED, 'the prompt'])

    async def run():
        runner = SpawnerRunner(default_cwd=str(tmp_path))
        return await runner.spawn('the prompt', model_or_combo=REQUESTED, role='review',
                                  client='opencode', cwd=str(tmp_path), dry_run=True)
    job = asyncio.run(run())
    cmd = job.command
    assert cmd.index('--title') < cmd.index('the prompt')
    assert cmd[cmd.index('--title') + 1] == 'plangraph-' + job.job_id


def test_standing_answer_no_tolerates_scope_quotes_and_still_needs_zero_hits():
    assert parse_test_gaming('TEST-GAMING: no - searched the "batch in" stamp: 0 hits\nVERDICT: pass') == ('no', None)
    assert parse_test_gaming('TEST-GAMING: no - searched the "batch in" stamp: 3 hits')[0] != 'no'
