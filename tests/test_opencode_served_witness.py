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


def test_opencode_tools_denied_requires_every_tool_deny(tmp_path):
    from plangraph.spawner.runner import _DENIED_TOOLS, _opencode_tools_denied
    # D-2921 F1: the proof now spans config AND env, so every case carries both
    # and still isolates the tool-deny condition.
    env = _deny_env(tmp_path)
    assert _opencode_tools_denied(json.dumps(_FULL_CFG), env)
    lax = {t: 'deny' for t in _DENIED_TOOLS}
    lax['edit'] = 'allow'
    assert not _opencode_tools_denied(json.dumps({'permission': lax, 'mcp': {}, 'plugin': []}), env)
    assert not _opencode_tools_denied(json.dumps({'permission': {'edit': 'deny'}, 'mcp': {}, 'plugin': []}), env)
    assert not _opencode_tools_denied('not json', env)
    assert not _opencode_tools_denied('', env)


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


# D-2921 F1: the deny block must hold against every other config layer.
from plangraph.spawner import opencode_overlay  # noqa: E402
from plangraph.spawner.runner import _DENIED_TOOLS  # noqa: E402
from plangraph.spawner import runner as runner_mod  # noqa: E402

_REAL_MANAGED_DIR = runner_mod._opencode_managed_dir  # before the autouse patch

_FULL_CFG = {'permission': {t: 'deny' for t in ('*',) + _DENIED_TOOLS}, 'mcp': {}, 'plugin': []}


@pytest.fixture(autouse=True)
def _empty_managed_dir(tmp_path, monkeypatch):
    """Hermetic: point opencode's managed config dir at an empty tmp dir."""
    managed = tmp_path / 'managed-opencode'
    managed.mkdir()
    monkeypatch.setattr(runner_mod, '_opencode_managed_dir', lambda env: managed)
    return managed


def _deny_env(home):
    """The isolation env the review block hands the child (R1)."""
    (home / 'pg-empty.json').write_text('{}')
    return {'HOME': str(home), 'XDG_CONFIG_HOME': str(home / 'config'),
            'XDG_DATA_HOME': str(home), 'OPENCODE_CONFIG_DIR': str(home),
            'OPENCODE_CONFIG': str(home / 'pg-empty.json'),
            'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
            'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1'}


def _freeze_leg(tmp_path, monkeypatch, seed, freeze=(), bad_cfg=None, extra=None):
    """A review leg whose review-block writes to `freeze` env keys are dropped
    and whose config stays `bad_cfg`: one hardening condition removed in turn.
    `extra` values are names under tmp_path (a relative path anchors there)."""
    extra = dict(extra or {})
    if bad_cfg is not None:
        extra['OPENCODE_CONFIG_CONTENT'] = bad_cfg
        freeze = set(freeze) | {'OPENCODE_CONFIG_CONTENT'}
    real = opencode_overlay.overlay_env

    def overlay(base):
        class Frozen(dict):
            def __setitem__(self, key, value):
                if key not in freeze:
                    super().__setitem__(key, value)

            def __delitem__(self, key):  # the OPENCODE_* drop is blocked too
                if key not in freeze:
                    super().__delitem__(key)
        return Frozen(real(base))

    monkeypatch.setattr(opencode_overlay, 'overlay_env', overlay)
    return _run_leg(tmp_path, monkeypatch, seed, extra_env=extra)


@pytest.mark.parametrize('freeze,bad_cfg,extra', [
    ((), json.dumps({**_FULL_CFG, 'mcp': {'evil': {}}}), {}),
    ((), json.dumps({**_FULL_CFG, 'plugin': ['evil']}), {}),
    ((), json.dumps({**_FULL_CFG, 'permission': {t: 'deny' for t in _DENIED_TOOLS}}), {}),
    (('OPENCODE_DISABLE_PROJECT_CONFIG',), None, {}),
    (('OPENCODE_DISABLE_CLAUDE_CODE',), None, {}),
    (('OPENCODE_DISABLE_CLAUDE_CODE_SKILLS',), None, {}),
    (('OPENCODE_CONFIG_DIR',), None, {}),
    (('OPENCODE_CONFIG',), None, {}),
    (('HOME',), None, {'HOME': 'elsewhere-home'}),
    (('XDG_CONFIG_HOME',), None, {'XDG_CONFIG_HOME': 'elsewhere-config'}),
    (('OPENCODE_CLI_CONFIG_CONTENT',), None, {'OPENCODE_CLI_CONFIG_CONTENT': '{"x": 1}'}),
    (('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'OPENCODE_CONFIG_DIR', 'OPENCODE_CONFIG'), None,
     {'HOME': 'jobhome', 'XDG_CONFIG_HOME': 'config', 'OPENCODE_CONFIG_DIR': 'jobhome',
      'OPENCODE_CONFIG': 'pg-bad.json'}),
    (('OPENCODE_CONFIG',), None, {'OPENCODE_CONFIG': 'outside.json'}),
], ids=['mcp', 'plugin', 'no_wildcard_deny', 'disable_project_config', 'disable_claude_code', 'disable_claude_code_skills',
        'config_dir', 'config_file', 'home_outside', 'xdg_config_home_outside', 'stray_opencode_env',
        'config_content_not_empty', 'config_outside_dir'])
def test_deny_block_condition_removed_yields_not_denied_and_no_witness(tmp_path, monkeypatch, freeze, bad_cfg, extra):
    (tmp_path / 'pg-bad.json').write_text('{"x": 1}')  # inside job dir, content not {}
    (tmp_path / 'outside.json').write_text('{}')       # content ok, outside job dir
    extra = {k: str(tmp_path / v) for k, v in (extra or {}).items()}
    job, _ = _freeze_leg(tmp_path, monkeypatch,
                         lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]),
                         freeze=freeze, bad_cfg=bad_cfg, extra=extra)
    assert {'event': 'opencode_tools_not_denied'} in job.events
    assert job.request_side_model is None
    assert job.witness_kind is None


def test_deny_block_all_conditions_present_denies_and_witnesses(tmp_path, monkeypatch):
    job, _ = _freeze_leg(tmp_path, monkeypatch,
                         lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]))
    assert {'event': 'opencode_tools_denied'} in job.events
    assert job.request_side_model == REQUESTED
    assert job.witness_kind == 'request-side-opencode-store'


@pytest.mark.parametrize('name', ['opencode.json', 'opencode.jsonc'])
def test_managed_config_file_yields_not_denied(tmp_path, monkeypatch, _empty_managed_dir, name):
    (_empty_managed_dir / name).write_text('{}')
    job, _ = _freeze_leg(tmp_path, monkeypatch,
                         lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]))
    assert {'event': 'opencode_tools_not_denied'} in job.events
    assert job.witness_kind is None


def test_managed_dir_per_platform_reads_the_child_env(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.sys, 'platform', 'win32')
    assert _REAL_MANAGED_DIR({'ProgramData': str(tmp_path)}) == tmp_path / 'opencode'
    assert _REAL_MANAGED_DIR({}) == Path('C:\\ProgramData', 'opencode')
    monkeypatch.setattr(runner_mod.sys, 'platform', 'linux')
    assert _REAL_MANAGED_DIR({'ProgramData': str(tmp_path)}) == Path('/etc/opencode')
    monkeypatch.setattr(runner_mod.sys, 'platform', 'darwin')
    assert _REAL_MANAGED_DIR({'ProgramData': str(tmp_path)}) == Path('/Library/Application Support/opencode')


def test_locked_store_db_returns_none_without_blocking(tmp_path):
    import threading
    import time
    _store(tmp_path, [('s1', 't')], [('s1', 'glm-5.3-flash-free')])
    db = tmp_path / 'opencode' / 'opencode.db'
    locker = sqlite3.connect(db)
    locker.execute('BEGIN EXCLUSIVE')
    out = {}

    def probe():
        start = time.monotonic()
        out['value'] = _opencode_served_model(db, 't')
        out['elapsed'] = time.monotonic() - start

    th = threading.Thread(target=probe, daemon=True)
    th.start()
    th.join(5)
    stalled = th.is_alive()
    locker.rollback()
    locker.close()
    th.join(2)
    assert not stalled, 'store read blocked on a locked db'
    assert out['value'] is None
    assert out['elapsed'] < 5


def _record_mkdtemp(monkeypatch):
    """Record each pg-ocdata dir the runner creates (TMPDIR tricks are banned)."""
    import tempfile
    made = []
    real = tempfile.mkdtemp

    def wrapper(*args, **kwargs):
        d = real(*args, **kwargs)
        made.append(d)
        return d

    monkeypatch.setattr(tempfile, 'mkdtemp', wrapper)
    return made


def test_dry_run_review_leg_removes_ocdata_dir(tmp_path, monkeypatch):
    made = _record_mkdtemp(monkeypatch)
    runner = SpawnerRunner(default_cwd=str(tmp_path))

    async def run():
        return await runner.spawn('p', model_or_combo=REQUESTED, role='review',
                                  client='opencode', cwd=str(tmp_path), dry_run=True)
    job = asyncio.run(run())
    assert job.dry_run
    assert made and all(Path(d).name.startswith('pg-ocdata-') for d in made)
    assert not any(Path(d).exists() for d in made)
    assert runner._ocdata_dirs == {}


def test_render_refused_review_leg_removes_ocdata_dir(tmp_path, monkeypatch):
    made = _record_mkdtemp(monkeypatch)
    monkeypatch.setattr('plangraph.routing_support.opencode_render.render_gateway_providers',
                        lambda *a, **k: 'mock render refusal')
    runner = SpawnerRunner(default_cwd=str(tmp_path))

    async def run():
        return await runner.spawn('p', model_or_combo=REQUESTED, role='review',
                                  client='opencode', cwd=str(tmp_path))
    job = asyncio.run(run())
    assert {'event': 'render_refused', 'reason': 'mock render refusal'} in job.events
    assert made and not any(Path(d).exists() for d in made)
    assert runner._ocdata_dirs == {}


def test_cancelled_monitor_task_removes_ocdata_dir(tmp_path, monkeypatch):
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)

    async def factory(*args, **kwargs):
        async def communicate(*a, **k):
            await asyncio.Event().wait()
        return SimpleNamespace(pid=123, returncode=None, communicate=communicate)

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('p', model_or_combo=REQUESTED, role='review',
                                 client='opencode', cwd=str(tmp_path),
                                 extra_env={}, wait_seconds=0)
        oc_dir = runner._ocdata_dirs[job.job_id]  # stored before cleanup
        assert Path(oc_dir).name.startswith('pg-ocdata-')
        # Cancelled between creation and its finally: the monitor never starts,
        # so its own finally can never clean up (D-2921 F3).
        runner._tasks[job.job_id].cancel()
        await asyncio.gather(runner._tasks[job.job_id], return_exceptions=True)
        await asyncio.sleep(0.2)
        return oc_dir
    oc_dir = asyncio.run(run())
    assert not Path(oc_dir).exists()
    assert runner._ocdata_dirs == {}


def test_review_child_env_drops_stray_opencode_names_and_rehomes_home(tmp_path, monkeypatch):
    """R1: a caller-set OPENCODE_* override never reaches the review child and
    HOME lands in the per-job dir."""
    job, ctx = _run_leg(tmp_path, monkeypatch,
                        lambda t, home: _store(home, [('s1', t)], [('s1', 'glm-5.3-flash-free')]),
                        extra_env={'OPENCODE_CLI_CONFIG_CONTENT': '{"x": 1}',
                                   'HOME': str(tmp_path / 'elsewhere')})
    env = ctx['env']
    assert 'OPENCODE_CLI_CONFIG_CONTENT' not in env
    assert Path(env['HOME']).resolve().is_relative_to(Path(env['XDG_DATA_HOME']).resolve())


def test_locked_store_read_does_not_stall_the_monitor_loop(tmp_path, monkeypatch):
    """R2: the store read is threaded -- a locked store blocks only its thread,
    and another coroutine still runs before the read returns."""
    import time

    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    done = {}
    real_read = _opencode_served_model

    def read(*args):
        out = real_read(*args)
        done['read_returned'] = time.monotonic()
        return out
    monkeypatch.setattr('plangraph.spawner.runner._opencode_served_model', read)
    lockers = []

    async def factory(*args, **kwargs):
        argv = list(args)
        data_home = Path(kwargs['env'].get('XDG_DATA_HOME') or '')
        _store(data_home, [('s1', argv[argv.index('--title') + 1])], [('s1', 'glm-5.3-flash-free')])
        locker = sqlite3.connect(data_home / 'opencode' / 'opencode.db')
        locker.execute('BEGIN EXCLUSIVE')  # hold the store locked across the read
        lockers.append(locker)

        async def communicate():
            return b'ok', b''
        return SimpleNamespace(pid=123, returncode=0, communicate=communicate)

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('prompt', model_or_combo=REQUESTED, role='review',
                                client='opencode', cwd=str(tmp_path),
                                extra_env={}, wait_seconds=0)

        async def nap():
            await asyncio.sleep(0.05)
            done['slept'] = time.monotonic()
        sleeper = asyncio.create_task(nap())
        await runner._tasks[job.job_id]
        await sleeper
    asyncio.run(run())
    for locker in lockers:
        locker.rollback()
        locker.close()
    assert 'read_returned' in done
    assert done['slept'] < done['read_returned'], 'monitor loop stalled on the locked-store read'


def test_unlocked_store_returns_the_model(tmp_path):
    """R3: positive control for the locked-store read: the same store, once
    unlocked, yields the model."""
    _store(tmp_path, [('s1', 't')], [('s1', 'glm-5.3-flash-free')])
    db = tmp_path / 'opencode' / 'opencode.db'
    locker = sqlite3.connect(db)
    locker.execute('BEGIN EXCLUSIVE')
    locker.rollback()
    locker.close()
    assert _opencode_served_model(db, 't') == 'opencode/glm-5.3-flash-free'
