"""Fixtures that keep a test's repository state inside its own tmp_path.

A `.git` left in a parent of the temp dir (for example /tmp/.git) changes how every
later test finds its repository root, so a leak fails the test that made it.
"""
import os
import tempfile

import pytest


def temp_ancestor_gits(tempdir=None):
    """Existing `.git` entries at the temp dir and above it (plain os.path: some tests fake os.name)."""
    here = os.path.realpath(tempdir or tempfile.gettempdir())
    found = set()
    while True:
        entry = os.path.join(here, '.git')
        if os.path.lexists(entry):
            found.add(entry)
        parent = os.path.dirname(here)
        if parent == here:
            return found
        here = parent


def leaked_gits(before, tempdir=None):
    """`.git` entries that appeared since ``before``; an empty directory is removed again."""
    leaked = sorted(temp_ancestor_gits(tempdir) - before)
    for entry in leaked:
        try:
            os.rmdir(entry)  # only an empty directory goes; a real repository stays
        except OSError:
            pass
    return leaked


@pytest.fixture(autouse=True)
def no_git_outside_tmp_path(request):
    before = temp_ancestor_gits()
    yield
    leaked = leaked_gits(before)
    assert not leaked, f'{request.node.nodeid} left a .git outside its tmp_path: {leaked}'


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
