import _thread
import faulthandler
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager
from threading import get_ident
from time import monotonic
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
    """Select blocking or quarantined tests as of a shared UTC run timestamp."""
    from datetime import datetime, timedelta

    if mode not in ('blocking', 'quarantine'):
        raise pytest.UsageError('PYTEST_QUARANTINE_MODE must be blocking or quarantine')
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
        if bool(owner) == (mode == 'quarantine'):
            selected.append(item)
    return selected, matched


def pytest_addoption(parser):
    parser.addini('test_deadline', 'Default per-test deadline in seconds', default='120')


# The deadline alarm uses bare _thread primitives captured at import: tests may patch
# threading.Thread, Thread.__init__ or Thread.daemon, and a Thread/Timer object built
# per phase would then fail to arm and leave that patch unrestored for later tests.
_START_ALARM_THREAD = _thread.start_new_thread
_NEW_LOCK = _thread.allocate_lock
_PTHREAD_KILL = getattr(signal, 'pthread_kill', None)
_INTERRUPT_MAIN = _thread.interrupt_main
# Platforms without a thread-directed signal (Windows) run the same handler through
# interrupt_main, which fires at the next bytecode boundary of the main thread.
_DEADLINE_SIGNAL = getattr(signal, 'SIGUSR2', signal.SIGINT)
_SET_SIGNAL_HANDLER = signal.signal
_DEADLINE = pytest.StashKey()


class _Alarm:
    """Send `sig` to thread `target` after `delay` seconds unless cancelled."""

    def __init__(self, delay, target, sig):
        self._cancelled = _NEW_LOCK()
        self._cancelled.acquire()
        self._finished = _NEW_LOCK()
        self._finished.acquire()
        self._delay = min(max(0, delay), _thread.TIMEOUT_MAX)
        self._target = target
        self._sig = sig
        self.started = False

    def _run(self):
        try:
            # The lock is only released by cancel(); a timeout means the deadline passed.
            if not self._cancelled.acquire(timeout=self._delay):
                if _PTHREAD_KILL is not None:
                    _PTHREAD_KILL(self._target, self._sig)
                else:
                    _INTERRUPT_MAIN(self._sig)
        finally:
            self._finished.release()

    def start(self):
        _START_ALARM_THREAD(self._run, ())
        self.started = True

    def cancel(self):
        if self._cancelled.locked():
            self._cancelled.release()

    def join(self):
        if self.started:
            self._finished.acquire()
            self._finished.release()


class _TestHung(BaseException):
    """Unwind the test without caching a timeout as a fixture setup failure."""


class _TeardownHung(_TestHung, pytest.fail.Exception):
    """Let pytest collect this teardown failure and run remaining finalizers."""


def _stop_hung_test(item, started, teardown):
    pid = os.getpid()
    message = f'HUNG {_ci_nodeid(item.nodeid)}'
    with tempfile.TemporaryFile() as stacks:
        faulthandler.dump_traceback(file=stacks, all_threads=True)
        stacks.seek(0)
        message += '\n' + stacks.read().decode(errors='replace')
    if started is not None:
        from plangraph.spawner import proc_kill

        table = proc_kill._scan()
        isolated_group = os.getpgrp() == pid
        # Shared fixtures can own children that predate this test.
        roots = {child for child, entry in table.items() if child != pid and
                 (entry[1] == pid or (isolated_group and entry[2] == pid)) and
                 entry[4] >= started}
        children = proc_kill.descendants(table, roots)
        try:
            proc_kill.kill_identities({child: table[child][4] for child in children if table[child][0] != 'Z'})
        except OSError as exc:
            message += f'Could not stop test subprocesses: {exc}\n'
    raise (_TeardownHung if teardown else _TestHung)(message)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    try:
        default = float(item.config.getini('test_deadline'))
        marker = item.get_closest_marker('long_test_deadline')
        seconds = float(marker.args[0]) if marker else default
        reason = marker.kwargs.get('reason') if marker else None
        if not math.isfinite(default) or default <= 0:
            raise ValueError
        if marker and (not math.isfinite(seconds) or seconds <= default or
                       not isinstance(reason, str) or not reason.strip()):
            raise ValueError
    except (IndexError, TypeError, ValueError) as exc:
        raise pytest.UsageError(
            f'{item.nodeid}: test_deadline must be positive; longer overrides need a reason',
        ) from exc
    started = (int(time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf('SC_CLK_TCK'))
               if sys.platform == 'linux' else None)
    item.stash[_DEADLINE] = (monotonic() + seconds, seconds, started)
    return (yield)


def _watch_test_phase(item, teardown=False):
    expires, seconds, started = item.stash[_DEADLINE]
    remaining = expires - monotonic()
    armed = False

    def interrupt(signum, frame):
        if armed:
            # Teardown must be able to clean up after a timeout and is bounded too.
            item.stash[_DEADLINE] = (monotonic() + seconds, seconds, started)
            _stop_hung_test(item, started, teardown)

    # SIGALRM belongs to tests that install their own subprocess alarms.
    sig = _DEADLINE_SIGNAL
    alarm = _Alarm(remaining, get_ident(), sig)
    arm_failure = None
    previous = _SET_SIGNAL_HANDLER(sig, interrupt)
    try:
        try:
            alarm.start()
            armed = True
        except Exception as exc:
            # The phase still runs, so the other hooks and fixtures can finish and
            # only this test reports the failure.
            arm_failure = exc
            faulthandler.dump_traceback_later(max(0.001, remaining))
        if armed and monotonic() >= expires:
            interrupt(sig, None)
        result = yield
    finally:
        armed = False
        alarm.cancel()
        alarm.join()
        if arm_failure is not None:
            faulthandler.cancel_dump_traceback_later()
        _SET_SIGNAL_HANDLER(sig, previous)
    if arm_failure is not None:
        raise RuntimeError(f'could not arm the test deadline: {arm_failure!r}') from arm_failure
    return result


pytest_runtest_setup = pytest_runtest_call = pytest.hookimpl(wrapper=True, tryfirst=True)(_watch_test_phase)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    return (yield from _watch_test_phase(item, teardown=True))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if call.excinfo is not None and call.excinfo.errisinstance(_TestHung):
        report.longrepr = str(call.excinfo.value)
    return report


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
