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


def _test_subreaper(enabled=None):
    import ctypes

    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    prctl.restype = ctypes.c_int
    get_child_subreaper, set_child_subreaper = 37, 36
    previous = ctypes.c_int()
    if enabled is None:
        # A worker adopts its orphaned grandchildren even across setsid().
        if prctl(get_child_subreaper, ctypes.addressof(previous), 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'cannot read pytest child subreaper setting')
        enabled = 1
    if prctl(set_child_subreaper, enabled, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot set pytest child subreaper')
    return previous.value


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    import faulthandler
    import json
    import time
    config = item.config
    if not hasattr(config, '_pg_deadline_watchdog'):
        if sys.platform.startswith('linux'):
            config._pg_previous_subreaper = _test_subreaper()
        helper = Path(__file__).resolve()
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
    if hasattr(config, '_pg_previous_subreaper'):
        _test_subreaper(config._pg_previous_subreaper)
    if hasattr(config, '_pg_deadline_stderr'):
        os.close(config._pg_deadline_stderr)


def _test_watchdog():
    """Monitor a pytest process over stdin, independently of its GIL."""
    import json
    import queue
    import signal
    import threading
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from plangraph.spawner.proc_kill import _descendants, _scan, _stat, alive

    parent = os.getppid()
    token = _stat(parent) if sys.platform.startswith('linux') else None
    events = queue.Queue()

    def read_events():
        for line in sys.stdin:
            events.put(json.loads(line))
        events.put(None)

    threading.Thread(target=read_events, daemon=True).start()
    while (event := events.get()) is not None:
        if not event:
            continue
        while event:
            node, deadline = event
            try:
                event = events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
        if event is None:
            return
        if not event:
            continue

        os.write(2, (f'HUNG {node}\n').encode('utf-8', 'backslashreplace'))
        if token is not None and alive(parent, token[4]):
            # Pytest adopts double-forked descendants as a subreaper. Freeze
            # their tree before killing its root so they cannot race reparenting.
            os.kill(parent, signal.SIGSTOP)
            stopped = {parent: token[4]}
            freeze_deadline = time.monotonic() + 0.25
            while True:
                procs = _scan()
                new = _descendants(procs, {parent}, {os.getpid()}).items() - stopped.items()
                for pid, start in new:
                    if alive(pid, start):
                        try:
                            os.kill(pid, signal.SIGSTOP)
                        except ProcessLookupError:
                            pass
                stopped.update(new)
                if not new and (time.monotonic() >= freeze_deadline or all(
                    pid not in procs or procs[pid][4] != start or procs[pid][0] in 'TtZX'
                    for pid, start in stopped.items()
                )):
                    break
                time.sleep(0.01)
            for pid, start in reversed(tuple(stopped.items())):
                if alive(pid, start):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        elif os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(parent)], timeout=10)
        elif sys.platform != 'linux' and os.getppid() == parent:
            os.kill(parent, signal.SIGKILL)
        return



if __name__ == '__main__' and sys.argv[1:] == ['--test-watchdog']:
    _test_watchdog()
