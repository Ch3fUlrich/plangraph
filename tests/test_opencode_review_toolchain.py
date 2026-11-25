"""An OpenCode review seat runs with cargo and the service's pytest on PATH
(LE-OPENCODE-REVIEW-SEAT-HAS-NO-CARGO-OR-PYTEST)."""
import asyncio
import os
import shutil
import site
import subprocess
import sys

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.spawner.launch_spec import _LaunchOwned
from plangraph.spawner.runner import SpawnerRunner
from test_opencode_isolation_go import source_store
from plangraph.spawner.opencode_isolation import GO_AUTH_DB_ENV


def _tool(path, body='#!/bin/sh\necho "$0 $@"\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
@pytest.mark.parametrize('leg_id', ['opencode-go-deepseek-v4-flash', 'opencode-go-glm-5.3-flash'])
def test_review_seat_env_resolves_cargo_and_python_pytest(tmp_path, monkeypatch, leg_id):
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)
    home = tmp_path / 'home'
    _tool(home / '.cargo/bin/cargo')
    # The interpreter may keep pytest in its user site; the seat reaches it through PYTHONUSERBASE.
    (home / '.local').symlink_to(site.getuserbase())
    # The service's interpreter lives in a venv that has pytest; the bare PATH python does not.
    venv = tmp_path / 'service-venv'
    _tool(venv / 'bin/pytest')
    _tool(venv / 'bin/python', f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    bare = tmp_path / 'bare/bin'
    _tool(bare / 'python', '#!/bin/sh\necho "No module named _pytest" >&2\nexit 1\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PATH', str(bare))
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    monkeypatch.setattr(sys, 'prefix', str(venv))
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    runner._child_envs = _LaunchOwned()
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    job = asyncio.run(runner.spawn('Review the change.', model_or_combo=leg.model, chosen=leg,
                                   role='review', cwd=str(checkout), dry_run=True, prepare_launch=True))
    env = runner._child_envs[job.job_id]
    assert env['HOME'] != str(home)  # the seat's own home hides the user's profile
    assert shutil.which('cargo', path=env['PATH']) == str(home / '.cargo/bin/cargo')
    python = shutil.which('python', path=env['PATH'])
    assert python == str(venv / 'bin/python')
    assert shutil.which('pytest', path=env['PATH']) == str(venv / 'bin/pytest')
    ran = subprocess.run([python, '-m', 'pytest', '--version'], env=env, capture_output=True, text=True)
    assert ran.returncode == 0 and 'pytest' in (ran.stdout + ran.stderr).lower(), ran.stderr


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_the_service_venv_is_first_and_not_shadowed_by_a_later_path_entry(tmp_path, monkeypatch):
    from plangraph.routing_support.claude_seat import review_tool_path
    venv = tmp_path / 'service-venv'
    _tool(venv / 'bin/pytest')
    monkeypatch.setattr(sys, 'prefix', str(venv))
    path = review_tool_path(os.pathsep.join(['/usr/bin', str(venv / 'bin')]), str(tmp_path))
    assert path.split(os.pathsep)[0] == str(venv / 'bin') and path.split(os.pathsep).count(str(venv / 'bin')) == 1


def test_review_seat_keeps_user_installs_and_rustup_reachable_after_the_home_swap(tmp_path, monkeypatch):
    from plangraph.spawner.opencode_isolation import review_seat_home
    home = tmp_path / 'home'
    for name in ('.local', '.rustup', '.cargo'):
        (home / name).mkdir(parents=True)
    job = tmp_path / 'job'
    job.mkdir()
    for name in ('PYTHONUSERBASE', 'RUSTUP_HOME', 'CARGO_HOME'):
        monkeypatch.delenv(name, raising=False)
    env = {'HOME': str(home), 'PATH': '/usr/bin'}
    review_seat_home(env, str(job))
    assert env['HOME'] == str(job)
    assert (env['PYTHONUSERBASE'], env['RUSTUP_HOME'], env['CARGO_HOME']) == (
        str(home / '.local'), str(home / '.rustup'), str(home / '.cargo'))
    # An explicit setting is kept, and a cargo home holding credentials is never handed to the seat.
    (home / '.cargo' / ('cred' + 'entials.toml')).write_text('x')
    job2 = tmp_path / 'job2'
    job2.mkdir()
    env = {'HOME': str(home), 'PATH': '/usr/bin', 'RUSTUP_HOME': '/custom/rustup'}
    review_seat_home(env, str(job2))
    assert env['RUSTUP_HOME'] == '/custom/rustup' and 'CARGO_HOME' not in env
