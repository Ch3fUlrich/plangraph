"""An OpenCode review seat runs with cargo and the repository's pytest on PATH
(LE-OPENCODE-REVIEW-SEAT-HAS-NO-CARGO-OR-PYTEST)."""
import asyncio
import os
import shutil
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
    # The service's interpreter lives in a venv that has pytest; the bare PATH python does not.
    venv = tmp_path / 'service-venv'
    _tool(venv / 'bin/pytest')
    _tool(venv / 'bin/python', '#!/bin/sh\necho "venv-python $@"\n')
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
