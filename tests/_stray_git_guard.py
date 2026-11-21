"""Fixtures that keep a test's repository state inside its own tmp_path.

A `.git` left in a parent of the temp dir (for example /tmp/.git) changes how every
later test finds its repository root, so a leak fails the test that made it.
"""
import os
import tempfile
import warnings
from contextlib import contextmanager

import pytest


def _watched_dirs(tempdir=None):
    """The process temp dir and, when set, pytest's basetemp (they can differ under --basetemp)."""
    dirs = [tempdir or tempfile.gettempdir()]
    if not tempdir and os.environ.get('PLANGRAPH_ROOT_CEILING'):
        dirs.append(os.environ['PLANGRAPH_ROOT_CEILING'])
    return dirs


def temp_ancestor_gits(tempdir=None):
    """Existing `.git` entries at the temp dirs and above them (plain os.path: some tests fake os.name)."""
    found = set()
    for start in _watched_dirs(tempdir):
        here = os.path.realpath(start)
        while True:
            entry = os.path.join(here, '.git')
            if os.path.lexists(entry):
                found.add(entry)
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
    return found


def leaked_gits(before, tempdir=None):
    """`.git` entries that appeared since ``before``; an empty directory is removed again."""
    leaked = sorted(temp_ancestor_gits(tempdir) - before)
    for entry in leaked:
        try:
            os.rmdir(entry)  # only an empty directory goes; a real repository stays
        except OSError:
            pass
    return leaked


def strict_mode():
    """CI also fails on a `.git` in a directory shared with other runs (the system temp root and above)."""
    return bool(os.environ.get('CI')) or os.environ.get('PLANGRAPH_STRAY_GIT_GUARD') == '1'


def _is_below(path, parent):
    return os.path.commonpath([path, parent]) == parent and path != parent


def owned_gits(found, tempdir=None):
    """Entries in directories this run owns: the pytest basetemp and its parent, and the process temp
    dir with the directories between it and the basetemp. A `.git` above the process temp dir
    (/tmp itself when TMPDIR is a private subdirectory) is shared with other sessions' runs."""
    temp = os.path.realpath(tempdir or tempfile.gettempdir())
    base = os.environ.get('PLANGRAPH_ROOT_CEILING') if not tempdir else None
    owned = {temp}
    if base:
        base = os.path.realpath(base)
        owned |= {base, os.path.dirname(base)}
        here = base
        while _is_below(here, temp):
            owned.add(here)
            here = os.path.dirname(here)
    return {entry for entry in found if os.path.dirname(entry) in owned}


@contextmanager
def watch_stray_git(nodeid, tempdir=None, strict=False):
    """Fail when the test leaves a `.git` in a directory this run owns; a `.git` in a shared directory
    fails only in strict mode and otherwise warns (another session's run can create it)."""
    before = temp_ancestor_gits(tempdir)
    yield
    leaked = leaked_gits(before, tempdir)
    owned = owned_gits(leaked, tempdir)
    message = f'{nodeid} left a .git outside its tmp_path: {leaked}'
    if owned or (leaked and strict):
        raise AssertionError(message)
    if leaked:
        warnings.warn(message, stacklevel=2)


@pytest.fixture(autouse=True)
def no_git_outside_tmp_path(request):
    with watch_stray_git(request.node.nodeid, strict=strict_mode()):
        yield


@pytest.fixture(scope='session', autouse=True)
def settings_root_ceiling(tmp_path_factory):
    """Settings root discovery stops at this session's basetemp, never walking into /tmp."""
    previous = os.environ.get('PLANGRAPH_ROOT_CEILING')
    os.environ['PLANGRAPH_ROOT_CEILING'] = str(tmp_path_factory.getbasetemp().resolve())
    yield
    if previous is None:
        os.environ.pop('PLANGRAPH_ROOT_CEILING', None)
    else:
        os.environ['PLANGRAPH_ROOT_CEILING'] = previous
