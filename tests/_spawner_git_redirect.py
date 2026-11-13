"""Hostile Git environment shared by checkout-bound operation tests."""
import os
import subprocess

import pytest


@pytest.fixture(params=['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT'])
def git_redirect(checkouts, monkeypatch, request):
    caller, redirect = checkouts[0], request.param
    values = {redirect: str(caller if redirect == 'GIT_WORK_TREE' else caller / '.git')}
    if redirect in ('GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        values[redirect] = str(caller / '.git' / 'objects')
    if redirect == 'GIT_CONFIG_PARAMETERS':
        values[redirect] = f"'core.worktree={caller}'"
    if redirect == 'GIT_CONFIG_COUNT':
        values = dict(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.worktree', GIT_CONFIG_VALUE_0=str(caller))
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    run = subprocess.run
    def checked(*args, **kwargs):
        clean = not values.keys() & kwargs.get('env', os.environ).keys()
        assert clean, 'Git redirect survived in subprocess environment'
        return run(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', checked)
