import os
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import contextmanager
import pytest

@contextmanager
def git_config_guard(root, registry_roots=None):
    """Watch shared/worktree config bytes without invoking Git on the checkout."""
    def snapshot():
        entry = Path(root) / '.git'
        metadata = entry
        if entry.is_file():
            metadata = (Path(root) / entry.read_text().removeprefix('gitdir:').strip()).resolve()
        common = metadata
        if (metadata / 'commondir').is_file():
            common = (metadata / (metadata / 'commondir').read_text().strip()).resolve()
        configs = tuple((str(path), path.read_bytes() if path.exists() else None)
                        for path in (common / 'config', metadata / 'config.worktree'))
        # The registry is what supplies `git worktree list`; read metadata directly.
        paths = [common / 'HEAD'] if registry_roots is None else []
        for entry in sorted((common / 'worktrees').glob('*')):
            if registry_roots is not None:
                try:
                    target = Path((entry / 'gitdir').read_text().strip()).resolve()
                except FileNotFoundError:
                    continue  # another session may remove its registration during this scan
                if not any(target.is_relative_to(Path(root).resolve()) for root in registry_roots):
                    continue
            paths.extend(entry / name for name in ('gitdir', 'HEAD', 'locked'))
        registry = tuple((str(path), path.read_bytes() if path.exists() else None) for path in paths)
        return configs, registry
    before = snapshot()
    try:
        yield
    finally:
        after = snapshot()
        if after[0] != before[0]:
            raise AssertionError('repository .git/config changed during pytest session')
        if after[1] != before[1]:
            raise AssertionError('repository worktree registry changed during pytest session')


def _test_environment(root, env):
    """Detach test subprocesses from every Git-advertised repository binding."""
    local = set(subprocess.check_output(
        ['git', 'rev-parse', '--local-env-vars'], cwd=root, env=env,
        text=True, stderr=subprocess.DEVNULL, timeout=30).upper().splitlines())
    local.update({'GIT_PREFIX', 'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES',
                  'GIT_DISCOVERY_ACROSS_FILESYSTEM'})
    return {key: value for key, value in env.items()
            if key.upper() not in local and not key.upper().startswith('GIT_CONFIG')}


def _clear_inherited_git_environment():
    clean = _test_environment(tempfile.gettempdir(), dict(os.environ))
    for key in set(os.environ) - clean.keys():
        del os.environ[key]
    os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    # Collection can import fixtures that invoke Git before session fixtures run.
    _clear_inherited_git_environment()


@pytest.fixture(scope='session', autouse=True)
def detached_git_environment():
    _clear_inherited_git_environment()


@pytest.fixture(scope='session', autouse=True)
def isolated_git_session(tmp_path_factory, detached_git_environment):
    config = tmp_path_factory.mktemp('git-session') / 'global.gitconfig'
    config.touch(mode=0o600)
    patch = pytest.MonkeyPatch()
    patch.setenv('GIT_CONFIG_GLOBAL', str(config))
    patch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    try:
        with git_config_guard(Path(__file__).resolve().parents[1], (tmp_path_factory.getbasetemp(),)):
            yield
    finally:
        patch.undo()


@pytest.fixture
def config_guard():
    return git_config_guard



import hashlib

def shard_items(items, shard):
    try:
        index, count = map(int, shard.split('/'))
        if not 0 <= index < count:
            raise ValueError
    except (ValueError, AttributeError):
        raise pytest.UsageError('PYTEST_SHARD must be INDEX/COUNT with 0 <= INDEX < COUNT') from None
    def bucket(item):
        group = item.get_closest_marker('xdist_group')
        key = str(group.kwargs.get('name', group.args[0] if group.args else 'default')) if group else item.nodeid
        return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big') % count
    return [item for item in items if bucket(item) == index]

@pytest.fixture
def shard_selector():
    return shard_items


def pytest_addoption(parser):
    parser.addini('test_deadline', 'Default test deadline in seconds', default='120')


def _test_deadline(item):
    import math

    seconds = float(item.config.getini('test_deadline'))
    mark = item.get_closest_marker('test_deadline')
    if mark is not None:
        if (mark not in item.own_markers or len(mark.args) != 1
                or not isinstance(mark.kwargs.get('reason'), str)
                or not mark.kwargs['reason'].strip()):
            raise pytest.UsageError(f'{item.nodeid}: test_deadline requires a named test and reason')
        seconds = float(mark.args[0])
    if not math.isfinite(seconds) or seconds <= 0:
        raise pytest.UsageError(f'{item.nodeid}: test_deadline must be finite and greater than zero')
    return seconds


def pytest_collection_finish(session):
    for item in session.items:
        _test_deadline(item)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    import faulthandler
    import json
    import time
    config = item.config
    if not hasattr(config, '_pg_deadline_watchdog'):
        helper = Path(__file__).resolve().parents[1] / 'src/plangraph/spawner/proc_kill.py'
        config._pg_deadline_watchdog = subprocess.Popen(
            [sys.executable, str(helper), '--test-watchdog'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=config._pg_deadline_stderr,
        )
    watchdog = config._pg_deadline_watchdog
    seconds = _test_deadline(item)
    faulthandler.dump_traceback_later(seconds, file=config._pg_deadline_stderr)
    try:
        # Give faulthandler's native timer time to emit all threads before the kill.
        watchdog.stdin.write(json.dumps([item.nodeid, time.monotonic() + seconds + 0.5]).encode() + b'\n')
        watchdog.stdin.flush()
        return (yield)
    finally:
        faulthandler.cancel_dump_traceback_later()
        watchdog.stdin.write(b'false\n')
        watchdog.stdin.flush()


def pytest_unconfigure(config):
    watchdog = getattr(config, '_pg_deadline_watchdog', None)
    if watchdog is not None:
        watchdog.stdin.close()
        watchdog.wait(timeout=5)
    if hasattr(config, '_pg_deadline_stderr'):
        os.close(config._pg_deadline_stderr)
