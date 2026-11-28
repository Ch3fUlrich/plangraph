import faulthandler
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from contextlib import contextmanager
import pytest

def python_test_env(env=None):
    """Return the repo interpreter and environment for a Python test subprocess."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ if env is None else env)
    venv = env.pop('VIRTUAL_ENV', None)
    env.pop('PYTHONHOME', None)
    bin_dir, executable = ('Scripts', 'python.exe') if os.name == 'nt' else ('bin', 'python')
    active_bin = Path(venv) / bin_dir if venv else None
    path = [entry for entry in env.get('PATH', os.defpath).split(os.pathsep)
            if Path(entry).is_absolute() and Path(entry) != active_bin
            and not (Path(entry).parent / 'pyvenv.cfg').is_file()]
    repo_venv = root / '.venv'
    interpreter = repo_venv / bin_dir / executable
    if interpreter.is_file():
        path.insert(0, str(interpreter.parent))
        env['VIRTUAL_ENV'] = str(repo_venv)
    else:
        # Keep the same Python version pytest runs, without the caller's venv.
        interpreter = Path(sys._base_executable)
    env['PATH'] = os.pathsep.join(path)
    env['PYTHONPATH'] = str(root / 'src')
    return str(interpreter), env


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
    def key(item):
        group = item.get_closest_marker('xdist_group')
        return str(group.kwargs.get('name', group.args[0] if group.args else 'default')) if group else item.nodeid

    if duration_file := os.environ.get('PYTEST_SHARD_DURATIONS_FILE'):
        # Imported here so a conftest that copies only this function still resolves it.
        from _split_ci_environment import _weighted_shard
        return _weighted_shard(items, key, index, count, duration_file)
    return [item for item in items
            if int.from_bytes(hashlib.sha256(key(item).encode()).digest()[:8], 'big') % count == index]


def _ci_nodeid(nodeid):
    # xdist's loadgroup adds an @group suffix after the normal test ID.
    suffix = nodeid.rfind('@')
    return nodeid[:suffix] if suffix > max(nodeid.rfind(']'), nodeid.rfind('::')) else nodeid


def _weighted_shard(items, key, index, count, duration_file):
    try:
        durations = json.loads(Path(duration_file).read_text(encoding='utf-8'))
        if not isinstance(durations, dict) or any(
            type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
            for seconds in durations.values()
        ):
            raise ValueError('expected a JSON object mapping test IDs to nonnegative seconds')
    except (OSError, ValueError, OverflowError) as exc:
        raise pytest.UsageError(f'PYTEST_SHARD_DURATIONS_FILE: {exc}') from exc

    weights = {}
    for item in items:
        group = key(item)
        weights[group] = weights.get(group, 0) + max(durations.get(_ci_nodeid(item.nodeid), 1), 0.001)
    loads = [0] * count
    buckets = {}
    for group in sorted(weights, key=lambda group: (-weights[group], group)):
        bucket = min(range(count), key=loads.__getitem__)
        buckets[group] = bucket
        loads[bucket] += weights[group]
    return [item for item in items if buckets[key(item)] == index]


@pytest.fixture
def shard_selector():
    return shard_items


def quarantine_items(items, ledger, mode, now):
    """Select blocking or quarantined tests as of a shared UTC run timestamp.

    Mode `xfail` keeps every test and marks the active quarantined ones non-strict xfail:
    they run and report, but their failure cannot fail the run."""
    from datetime import datetime, timedelta

    if mode not in ('blocking', 'quarantine', 'xfail'):
        raise pytest.UsageError('PYTEST_QUARANTINE_MODE must be blocking, quarantine or xfail')
    try:
        entries = json.loads(ledger.read_text(encoding='utf-8'))
        if not isinstance(entries, dict) or now.utcoffset() != timedelta():
            raise ValueError('expected a test ID mapping and a UTC run timestamp')
        active = {}
        for nodeid, entry in entries.items():
            owner = entry['owner']
            starts = datetime.fromisoformat(entry['starts'])
            expires = datetime.fromisoformat(entry['expires'])
            if (not isinstance(owner, str) or not owner.startswith('plangraph:') or
                not owner.removeprefix('plangraph:').strip() or
                starts.utcoffset() != timedelta() or expires.utcoffset() != timedelta() or
                not timedelta() < expires - starts <= timedelta(hours=48)):
                raise ValueError(f'invalid owner or quarantine window for {nodeid}')
            if starts <= now < expires:
                active[nodeid] = owner
    except (OSError, TypeError, KeyError, ValueError, OverflowError) as exc:
        raise pytest.UsageError(f'PYTEST_QUARANTINE_FILE: {exc}') from exc
    matched = {}
    selected = []
    for item in items:
        nodeid = _ci_nodeid(item.nodeid)
        owner = active.get(nodeid)
        if owner:
            matched[nodeid] = owner
            if mode == 'xfail':
                item.add_marker(pytest.mark.xfail(strict=False, reason=f'QUARANTINED owner={owner}'))
        if mode == 'xfail' or bool(owner) == (mode == 'quarantine'):
            selected.append(item)
    return selected, matched


def pytest_terminal_summary(terminalreporter, exitstatus):
    """Publish complete, canonical setup/call/teardown timings for this shard."""
    destination = os.environ.get('PYTEST_SHARD_DURATIONS_OUT')
    if not destination or exitstatus != pytest.ExitCode.OK:
        return
    durations = {}
    for reports in terminalreporter.stats.values():
        for report in reports:
            if isinstance(report, pytest.TestReport):
                nodeid = _ci_nodeid(report.nodeid)
                durations[nodeid] = durations.get(nodeid, 0) + report.duration
    target = Path(destination)
    fd, name = tempfile.mkstemp(dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(durations, stream, sort_keys=True)
            stream.write('\n')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


# Per-test timeout: a hang fails by test name instead of eating the CI job timeout.
DEFAULT_TEST_TIMEOUT = 300.0
# The faulthandler backstop fires this much later than the timeout: it catches a hang that never
# returns to the interpreter, dumps every thread's stack and exits the process (xdist then
# reports the crashed worker with the running test's id).
TIMEOUT_BACKSTOP_GRACE = 60.0
_ARMED = {}


def phase_timeout_seconds(item, environ=os.environ):
    """Budget of each setup, call and teardown phase: `@pytest.mark.timeout(seconds)`, else
    `PYTEST_TEST_TIMEOUT`, else 300 s; 0 turns the timeout off."""
    marker = item.get_closest_marker('timeout')
    raw = marker.args[0] if marker and marker.args else environ.get('PYTEST_TEST_TIMEOUT', DEFAULT_TEST_TIMEOUT)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = math.nan
    if not 0 <= seconds < math.inf:
        raise pytest.UsageError(f'test timeout of {item.nodeid}: {raw!r} is not a number of seconds')
    return seconds


def _signal_timer():
    return hasattr(signal, 'setitimer') and threading.current_thread() is threading.main_thread()


def _on_timeout(signum, frame):
    deadline = _ARMED.get('deadline')
    # A SIGALRM the test sends itself before the deadline is not this timer's.
    if deadline is None or time.monotonic() < deadline - 0.05:
        return
    pytest.fail(f"TEST-TIMEOUT {_ARMED['nodeid']} ({_ARMED['when']}) exceeded {_ARMED['seconds']:g}s")


def _arm(seconds):
    _ARMED['deadline'] = time.monotonic() + seconds
    if _signal_timer():
        signal.signal(signal.SIGALRM, _on_timeout)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    faulthandler.dump_traceback_later(seconds + TIMEOUT_BACKSTOP_GRACE, exit=True)


@contextmanager
def phase_timeout(item, when):
    """Fail the running phase with `TEST-TIMEOUT <nodeid>` once its budget is spent."""
    seconds = phase_timeout_seconds(item)
    if not seconds:
        yield
        return
    uses_signal = _signal_timer()
    previous = signal.getsignal(signal.SIGALRM) if uses_signal else None
    _ARMED.update(nodeid=item.nodeid, when=when, seconds=seconds)
    _arm(seconds)
    try:
        yield
    finally:
        _ARMED.clear()
        faulthandler.cancel_dump_traceback_later()
        if uses_signal:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_DFL if previous is None else previous)


@contextmanager
def timeout_extended(seconds):
    """Give a slow step (a cold cargo build) its own budget; its time does not count against
    the running test. Outside an armed test phase this does nothing."""
    deadline = _ARMED.get('deadline')
    if deadline is None:
        yield
        return
    remaining = max(deadline - time.monotonic(), 1.0)
    _ARMED['seconds'] = max(seconds, remaining)
    _arm(_ARMED['seconds'])
    try:
        yield
    finally:
        if _ARMED:
            _ARMED['seconds'] = remaining
            _arm(remaining)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    with phase_timeout(item, 'setup'):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    with phase_timeout(item, 'call'):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    with phase_timeout(item, 'teardown'):
        return (yield)
