"""Fixtures that keep a test's repository state inside its own tmp_path.

A `.git` left in a parent of the temp dir (for example /tmp/.git) changes how every
later test finds its repository root.

Attribution: a pytest worker runs one test at a time and owns its basetemp and temp dir, so a
`.git` that appears in those directories is that test's: the test fails and an empty stray is
removed. A `.git` above the process temp dir (the system temp root under a private TMPDIR) is
shared with every other worker and session, so it is never attributed to one test and never
removed here: the session checks it once at its end (a warning, or a failure in CI or with
`PLANGRAPH_STRAY_GIT_GUARD=1`).
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


def strict_mode():
    """CI also fails the session on a `.git` in a directory shared with other runs."""
    return bool(os.environ.get('CI')) or os.environ.get('PLANGRAPH_STRAY_GIT_GUARD') == '1'


def _is_below(path, parent):
    return os.path.commonpath([path, parent]) == parent and path != parent


def owned_gits(found, tempdir=None):
    """Entries in directories this worker owns: the pytest basetemp (and its parent, unless it is an xdist
    worker's, which its siblings share), and the process temp dir with the directories between it and the
    basetemp."""
    temp = os.path.realpath(tempdir or tempfile.gettempdir())
    base = os.environ.get('PLANGRAPH_ROOT_CEILING') if not tempdir else None
    owned = {temp}
    if base:
        base = os.path.realpath(base)
        owned.add(base)
        if not os.path.basename(base).startswith('popen-gw'):
            owned.add(os.path.dirname(base))
            here = base
            while _is_below(here, temp):
                owned.add(here)
                here = os.path.dirname(here)
        # An xdist worker's basetemp sits in a tree its sibling workers share: only the basetemp is its own.
    return {entry for entry in found if os.path.dirname(entry) in owned}


def shared_gits(tempdir=None):
    """Entries above the run's own directories: shared with every other worker and session."""
    found = temp_ancestor_gits(tempdir)
    return found - owned_gits(found, tempdir)


@contextmanager
def watch_stray_git(nodeid, tempdir=None):
    """Fail the test that leaves a `.git` in a directory its run owns; an empty stray is removed."""
    before = owned_gits(temp_ancestor_gits(tempdir), tempdir)
    yield
    leaked = sorted(owned_gits(temp_ancestor_gits(tempdir), tempdir) - before)
    for entry in leaked:
        try:
            os.rmdir(entry)  # only an empty directory goes; a real repository stays
        except OSError:
            pass
    assert not leaked, f'{nodeid} left a .git outside its tmp_path: {leaked}'


def check_shared(before, strict, tempdir=None):
    """Session end: a `.git` that appeared in a shared directory is reported once, never removed."""
    leaked = sorted(shared_gits(tempdir) - before)
    message = f'a .git appeared in a directory shared with other runs during this session: {leaked}'
    if leaked and strict:
        raise AssertionError(message)
    if leaked:
        warnings.warn(message)
    return leaked


@pytest.fixture(autouse=True)
def no_git_outside_tmp_path(request):
    with watch_stray_git(request.node.nodeid):
        yield


@pytest.fixture(scope='session', autouse=True)
def settings_root_ceiling(tmp_path_factory):
    """Settings root discovery stops at this session's basetemp, never walking into /tmp."""
    previous = os.environ.get('PLANGRAPH_ROOT_CEILING')
    os.environ['PLANGRAPH_ROOT_CEILING'] = str(tmp_path_factory.getbasetemp().resolve())
    # One checker per session: the only worker of an xdist run named gw0, or the plain process.
    checks = os.environ.get('PYTEST_XDIST_WORKER', 'gw0') == 'gw0'
    shared = shared_gits()
    yield
    if checks:
        check_shared(shared, strict_mode())
    if previous is None:
        os.environ.pop('PLANGRAPH_ROOT_CEILING', None)
    else:
        os.environ['PLANGRAPH_ROOT_CEILING'] = previous
