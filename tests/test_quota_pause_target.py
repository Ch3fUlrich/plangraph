"""Quota flag reads are hermetic and fail closed on unknown filesystem state."""
from collections import UserDict
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner.target import quota_paused


@pytest.mark.parametrize('state', [None, ''])
@pytest.mark.parametrize('source', ['explicit-env', 'xdg', 'home'])
def test_empty_state_defaults(tmp_path, monkeypatch, state, source):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_STATE_HOME', '' if source == 'home' else str(tmp_path / 'xdg'))
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path / 'explicit') if source == 'explicit-env' else '')
    root = {'explicit-env': tmp_path / 'explicit', 'xdg': tmp_path / 'xdg/plangraph',
            'home': tmp_path / 'home/.local/state/plangraph'}[source]
    root.mkdir(parents=True)
    (root / 'quota-pause').touch()
    monkeypatch.chdir(tmp_path)
    assert quota_paused(state, 'review')
    assert not quota_paused(tmp_path / 'unrelated', 'review')


@pytest.mark.parametrize('suffix', ['', '-1', '-001', '-0001', '-00012'])
def test_exempt_numbered_roles(tmp_path, suffix):
    (tmp_path / 'quota-pause').touch()
    assert not quota_paused(tmp_path, 'basic-analysis' + suffix)
    assert quota_paused(tmp_path, 'implement' + suffix)
    assert quota_paused(tmp_path, 'basic-analysis-0001-extra')


@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling-link', 'link-loop'])
def test_flag_presence_and_reset(tmp_path, kind):
    flag = tmp_path / 'quota-pause'
    if kind == 'directory':
        flag.mkdir()
    elif kind in ('dangling-link', 'link-loop'):
        flag.symlink_to(tmp_path / 'missing' if kind == 'dangling-link' else flag)
    else:
        flag.write_text('contents are not parsed')
    assert quota_paused(tmp_path, 'review')
    flag.rmdir() if kind == 'directory' else flag.unlink()
    assert not quota_paused(tmp_path, 'review')


@pytest.mark.parametrize('error', [errno.EACCES, errno.EPERM, errno.EIO, errno.ELOOP, errno.ENOTDIR])
def test_stat_errors_pause(tmp_path, monkeypatch, error):
    real_lstat = os.lstat
    flag = tmp_path / 'quota-pause'

    def fail(path, *args, **kwargs):
        if Path(path) == flag:
            raise OSError(error, 'injected stat failure')
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'lstat', fail)
    assert quota_paused(tmp_path, 'review')
    assert not quota_paused(tmp_path, 'basic-analysis')


def test_non_directory_state_pauses(tmp_path):
    state = tmp_path / 'state'
    state.touch()
    assert quota_paused(state, 'review')


@pytest.mark.parametrize('factory', [dict, UserDict, lambda **kw: SimpleNamespace(**kw)])
@pytest.mark.parametrize('fields,paused', [
    ({'id': 'claude-test'}, True), ({'model': 'claude-test'}, True),
    ({'id': 'other', 'model': 'claude-test'}, True),
    ({'id': 'other', 'model': 'other'}, False), ({'model': 'other'}, False),
    ({}, True), ({'id': None, 'model': ''}, True), ({'id': 42}, True),
])
def test_leg_metadata(tmp_path, factory, fields, paused):
    (tmp_path / 'quota-pause').touch()
    assert quota_paused(tmp_path, 'review', factory(**fields)) is paused
    (tmp_path / 'quota-pause').unlink()
    assert not quota_paused(tmp_path, 'review', factory(**fields))


def test_spawner_doc_size_and_quota_contract():
    doc = (Path(__file__).resolve().parents[1] / 'docs/SPAWNER.md').read_text()
    assert len(doc.splitlines()) <= 400
    for term in ('quota_pause', 'unknown-provider', 'numeric suffix', 'read errors', '%h'):
        assert term in doc
