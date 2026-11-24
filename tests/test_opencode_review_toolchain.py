"""An OpenCode review seat runs with cargo and the repository's pytest on PATH
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
    # The venv's interpreter is the real one, so `python -m pytest --version` really imports pytest.
    _tool(venv / 'bin/python', f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    bare = tmp_path / 'bare/bin'
    _tool(bare / 'python', '#!/bin/sh\necho "No module named _pytest" >&2\nexit 1\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PATH', str(bare))
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    monkeypatch.delenv('PREFLIGHT_PYTHON', raising=False)
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
    assert python != str(bare / 'python')  # the seat's python is the validated interpreter's wrapper
    assert shutil.which('pytest', path=env['PATH']) == str(venv / 'bin/pytest')
    ran = subprocess.run([python, '-m', 'pytest', '--version'], env=env, capture_output=True, text=True)
    assert ran.returncode == 0 and 'pytest' in (ran.stdout + ran.stderr).lower(), ran.stderr


def _interpreter(directory, body=None):
    """An interpreter in ``directory`` that really runs pytest, as the seat's trusted one."""
    _tool(directory / 'python', body or f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    return directory / 'python'


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_the_configured_interpreter_dir_is_first_and_the_checkout_venv_is_never_run(tmp_path, monkeypatch):
    from plangraph.routing_support.claude_seat import review_tool_path
    marker = tmp_path / 'checkout-venv-ran'
    checkout = tmp_path / 'checkout'
    _tool(checkout / '.venv/bin/pytest')
    _tool(checkout / '.venv/bin/python', f'#!/bin/sh\ntouch {marker}\nexit 0\n')  # the code under review
    (tmp_path / 'service/bin').mkdir(parents=True)
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / 'service'))
    monkeypatch.chdir(checkout)
    monkeypatch.setenv('PYTHONUSERBASE', site.getuserbase())
    monkeypatch.delenv('PREFLIGHT_PYTHON', raising=False)
    assert str(checkout / '.venv/bin') not in review_tool_path('/usr/bin', str(tmp_path), str(tmp_path / 'job/bin')).split(os.pathsep)
    other = tmp_path / 'other/bin'
    monkeypatch.setenv('PREFLIGHT_PYTHON', str(_interpreter(other)))
    venv_bin = str(checkout / '.venv/bin')
    path = review_tool_path(os.pathsep.join(['/usr/bin', str(other)]), str(tmp_path), str(tmp_path / 'job/bin'))
    assert path.split(os.pathsep)[:2] == [str(tmp_path / 'job/bin'), str(other)] and path.count(str(other)) == 1
    assert venv_bin not in path and not marker.exists()


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


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_the_harness_is_resolved_on_the_launch_path_not_the_interpreter_dir(tmp_path, monkeypatch):
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'opencode-go-deepseek-v4-flash')
    trusted = tmp_path / 'trusted'
    _tool(trusted / 'opencode')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    interpreter = _interpreter(tmp_path / 'interp/bin')
    _tool(interpreter.parent / 'opencode')  # a harness planted next to the interpreter the seat gets
    monkeypatch.setenv('PREFLIGHT_PYTHON', str(interpreter))
    monkeypatch.setenv('PYTHONUSERBASE', site.getuserbase())
    monkeypatch.setenv('PATH', f'{trusted}{os.pathsep}/usr/bin')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))
    started = []

    async def factory(exe, *args, **kwargs):
        started.append((exe, kwargs['env']['PATH']))
        raise RuntimeError('stop before exec')

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.spawn('Review the change.', model_or_combo=leg.model, chosen=leg,
                                 role='review', cwd=str(checkout)))
    exe, path = started[0]
    assert exe == str(trusted / 'opencode')
    assert str(interpreter.parent) in path.split(os.pathsep)  # pytest still comes from the interpreter's dir


@pytest.mark.real_probe
@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_host_admission_never_runs_a_harness_from_the_interpreter_dir(tmp_path, monkeypatch):
    from plangraph.spawner import resolver
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'opencode-go-deepseek-v4-flash')
    marker = tmp_path / 'planted-ran'
    trusted = tmp_path / 'trusted'
    listing = f'#!/bin/sh\necho {leg.model}\n'
    _tool(trusted / 'opencode', listing)
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    interpreter = _interpreter(tmp_path / 'interp/bin')
    _tool(interpreter.parent / 'opencode', f'#!/bin/sh\ntouch {marker}\necho {leg.model}\n')
    monkeypatch.setenv('PREFLIGHT_PYTHON', str(interpreter))
    monkeypatch.setenv('PYTHONUSERBASE', site.getuserbase())
    monkeypatch.setenv('PATH', f'{trusted}{os.pathsep}/usr/bin{os.pathsep}/bin')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))
    resolver.served_models.cache_clear()

    async def factory(*args, **kwargs):
        raise RuntimeError('stop before exec')

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.spawn('Review the change.', model_or_combo=leg.model, chosen=leg,
                                 role='review', cwd=str(checkout)))
    assert not marker.exists()


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_a_configured_interpreter_without_a_pytest_script_gets_a_python_on_the_seat_path(tmp_path, monkeypatch):
    from plangraph.routing_support.claude_seat import review_tool_path
    configured = tmp_path / 'sys/bin'  # like /usr/bin: python3 only, pytest in the user site
    _tool(configured / 'python3', f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    bare = tmp_path / 'bare/bin'
    _tool(bare / 'python', '#!/bin/sh\necho "No module named pytest" >&2\nexit 1\n')
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / 'service'))
    monkeypatch.setenv('PREFLIGHT_PYTHON', str(configured / 'python3'))
    monkeypatch.setenv('PYTHONUSERBASE', site.getuserbase())
    path = review_tool_path(str(bare), str(tmp_path), str(tmp_path / 'job/bin'))
    python = shutil.which('python', path=path)
    assert python == str(tmp_path / 'job/bin/python')
    ran = subprocess.run([python, '-m', 'pytest', '--version'], capture_output=True, text=True)
    assert ran.returncode == 0 and 'pytest' in (ran.stdout + ran.stderr).lower()


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_the_seat_python_is_the_validated_interpreter_never_a_sibling(tmp_path, monkeypatch):
    from plangraph.routing_support.claude_seat import review_tool_path
    configured = tmp_path / 'sys/bin'
    _tool(configured / 'python3', f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    _tool(configured / 'python', '#!/bin/sh\nexit 3\n')  # a sibling that was never validated
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / 'service'))
    monkeypatch.setenv('PREFLIGHT_PYTHON', str(configured / 'python3'))
    monkeypatch.setenv('PYTHONUSERBASE', site.getuserbase())
    path = review_tool_path('/usr/bin', str(tmp_path), str(tmp_path / 'job/bin'))
    for name in ('python', 'python3'):
        ran = subprocess.run([shutil.which(name, path=path), '-m', 'pytest', '--version'], capture_output=True, text=True)
        assert ran.returncode == 0, (name, ran.stderr)


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_a_pytest_script_beside_an_interpreter_that_cannot_import_pytest_is_not_enough(tmp_path, monkeypatch):
    from plangraph.routing_support.claude_seat import review_tool_path
    service = tmp_path / 'service'
    _tool(service / 'bin/pytest')
    _tool(service / 'bin/python', '#!/bin/sh\necho "No module named pytest" >&2\nexit 1\n')
    monkeypatch.setattr(sys, 'prefix', str(service))
    monkeypatch.delenv('PREFLIGHT_PYTHON', raising=False)
    monkeypatch.setattr('plangraph.routing_support.claude_seat.__file__', str(tmp_path / 'a/b/c/d/claude_seat.py'))
    path = review_tool_path('/usr/bin', str(tmp_path), str(tmp_path / 'job/bin'))
    assert str(service / 'bin') not in path.split(os.pathsep) and not (tmp_path / 'job/bin').exists()


@pytest.mark.skipif(os.name != 'posix', reason='shell stubs')
def test_launch_preparation_never_runs_the_review_checkout_interpreter(tmp_path, monkeypatch):
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == 'opencode-go-deepseek-v4-flash')
    marker = tmp_path / 'checkout-python-ran'
    checkout = tmp_path / 'checkout'
    _tool(checkout / '.venv/bin/pytest')
    _tool(checkout / '.venv/bin/python', f'#!/bin/sh\ntouch {marker}\nexit 0\n')
    (tmp_path / 'service/bin').mkdir(parents=True)
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / 'service'))
    monkeypatch.delenv('PREFLIGHT_PYTHON', raising=False)
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    monkeypatch.chdir(checkout)  # the spawner's own cwd is the checkout, too
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))
    runner = SpawnerRunner(default_cwd=str(checkout))
    runner._child_envs = _LaunchOwned()
    job = asyncio.run(runner.spawn('Review the change.', model_or_combo=leg.model, chosen=leg,
                                   role='review', cwd=str(checkout), dry_run=True, prepare_launch=True))
    assert str(checkout / '.venv/bin') not in runner._child_envs[job.job_id]['PATH'].split(os.pathsep)
    assert not marker.exists()
