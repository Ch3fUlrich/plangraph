"""A host with a data drive points every launcher at it by one setting (LE-DELL-DISK-EMERGENCY-SIXTH-TARGET-ON-ROOT)."""
import asyncio
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Event
from types import SimpleNamespace

import pytest

from plangraph.spawner import host_ready as h


def test_spawner_resolves_the_configured_target_and_never_creates_the_home_cache(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data" / "cargo-target"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(data))
    assert h.shared_cargo_target() == data
    env = h.cargo_child_env({"PATH": "/usr/bin"})
    assert env["CARGO_TARGET_DIR"] == str(data)
    if sys.platform.startswith("linux"):
        lease = h.cargo_lease()
        lease.close()
        assert (tmp_path / "data" / "cargo-target.plangraph-lock").exists()
    assert not (home / ".cache").exists()


def test_unset_setting_keeps_the_default_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLANGRAPH_CARGO_TARGET_DIR", raising=False)
    assert h.shared_cargo_target() == tmp_path / ".cache/plangraph/cargo-target"


@pytest.mark.parametrize("value", ["~/data/target", "/mnt/data/cargo"])
def test_home_expansion_and_absolute_values(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", value)
    expected = tmp_path / "data/target" if value.startswith("~") else __import__("pathlib").Path(value)
    assert h.shared_cargo_target() == expected


@pytest.mark.parametrize(('args', 'minimum', 'maximum'), [
    ('wait=0.2', 0.2, 1.0),
    ('', 5.0, 6.0),
])
def test_cross_process_cargo_lease_deadline(tmp_path, args, minimum, maximum):
    target = tmp_path / 'target'
    env = dict(os.environ, PLANGRAPH_CARGO_TARGET_DIR=str(target),
               PYTHONPATH=str(Path(h.__file__).resolve().parents[2]))
    holder = 'from plangraph.spawner.host_ready import cargo_lease\n'
    holder += 'with cargo_lease(exclusive=True):\n print("ready", flush=True)\n input()'
    waiter = f"""
import json, time
from plangraph.spawner.host_ready import cargo_lease
started = time.monotonic()
try:
    with cargo_lease({args}):
        raise AssertionError("busy lock admitted")
except OSError as exc:
    print(json.dumps(dict(elapsed=time.monotonic() - started, **exc.evidence)))
"""
    with subprocess.Popen([sys.executable, '-c', holder], env=env, text=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        try:
            # A separate read thread also bounds holder startup on Windows.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor() as executor:
                ready = executor.submit(process.stdout.readline)
                try:
                    assert ready.result(timeout=5).strip() == 'ready'
                except BaseException:
                    process.kill()
                    raise
            out = subprocess.check_output([sys.executable, '-c', waiter], env=env,
                                          text=True, timeout=maximum + 0.5)
            evidence = json.loads(out)
            assert minimum - 0.001 <= evidence['elapsed'] <= maximum, evidence
            assert evidence['code'] == 'cargo_lease_timeout'
            assert evidence['wait_budget_ms'] == round(minimum * 1000)
            assert evidence['contention_observed'] is True
            lock = target.with_name(target.name + '.plangraph-lock')
            assert Path(evidence['lock_path']) == lock.resolve()
            if os.name == 'posix':
                assert evidence['lock_identity'] == dict(device=lock.stat().st_dev, inode=lock.stat().st_ino)
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.communicate('\n', timeout=5)
    with h._cargo_lease_for(target, wait=0):
        pass


def test_shared_readers_and_ancestor_aliases_use_one_lock(tmp_path, monkeypatch):
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        if os.name == 'nt' and exc.winerror == 1314:  # symlink privilege is optional on Windows
            pytest.skip('Windows symlink privilege unavailable')
        raise
    target = real / 'target'
    with h._cargo_lease_for(target), h._cargo_lease_for(alias / 'target'):
        with pytest.raises(OSError) as blocked:
            h._cargo_lease_for(target, exclusive=True)
        assert not isinstance(blocked.value, h.CargoLeaseTimeout)
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(alias / 'target'))
    assert h.cargo_child_env({})['CARGO_TARGET_DIR'] == str(target.resolve())
    target.mkdir()
    leaf = tmp_path / 'leaf'
    leaf.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match='Cargo target leaf must be a plain directory'):
        h._cargo_lease_for(leaf)


def test_reclaim_locks_its_target_and_preserves_other_readers(tmp_path, monkeypatch):
    configured, other, checkout = (tmp_path / name for name in ('configured', 'other', 'checkout'))
    for path in (other, checkout):
        path.mkdir()
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(configured))
    with h.cargo_lease():
        with h._cargo_lease_for(other):
            with pytest.raises(OSError):
                h.reclaim_cargo_target(other, 0, checkout, wait=0)
            assert other.is_dir()
        h.reclaim_cargo_target(other, 0, checkout, wait=0)
    assert not other.exists()


def test_non_contention_error_retains_its_os_cause(tmp_path):
    target = tmp_path / 'target'
    # Opening a directory as a lock fails before any OS lock attempt.
    target.with_suffix('.plangraph-lock').mkdir()
    with pytest.raises(OSError) as failure:
        h._cargo_lease_for(target)
    assert not isinstance(failure.value, h.CargoLeaseTimeout)
    assert failure.value.errno in (errno.EISDIR, errno.EACCES)


@pytest.mark.parametrize(('wait', 'deadline', 'budget', 'attempted'), [
    (None, None, 5.0, True),
    (None, 100.23, 0.23, True),
    (0, None, 0, True),
    (0, 100.0, 0, False),
])
def test_lease_deadline_clips_every_attempt_and_pause(tmp_path, monkeypatch, wait, deadline, budget, attempted):
    from plangraph.spawner import probe_cleanup
    now = 100.0
    calls, pauses = [], []

    def pause(seconds):
        nonlocal now
        pauses.append(seconds)
        now += seconds

    def busy(path, exclusive):
        calls.append(now)
        return None, BlockingIOError(errno.EAGAIN, 'busy'), 'unavailable'

    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(h, 'time', clock)
    monkeypatch.setattr(probe_cleanup, 'time', clock)
    monkeypatch.setattr(probe_cleanup, 'Event', lambda: SimpleNamespace(is_set=lambda: False, wait=pause))
    monkeypatch.setattr(h, '_try_cargo_lock', busy)
    with pytest.raises(h.CargoLeaseTimeout) as failure:
        h._cargo_lease_for(tmp_path / 'target', wait=wait, deadline=deadline)
    assert bool(calls) is attempted
    assert all(at < 100 + budget for at in calls[1:])
    assert all(0 < seconds <= 0.1 for seconds in pauses)
    assert sum(pauses) == pytest.approx(budget)
    assert failure.value.evidence['wait_budget_ms'] == round(budget * 1000)
    assert failure.value.evidence['contention_observed'] is attempted
    assert failure.value.evidence['lock_identity'] == 'unavailable'


@pytest.mark.parametrize('before_transfer', [False, True])
def test_cancelled_async_acquisition_closes_handle(tmp_path, monkeypatch, before_transfer):
    from plangraph.spawner import probe_cleanup

    target = tmp_path / 'target'
    acquired, release = Event(), Event()
    real_lease = h.cargo_lease
    handles = []
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))

    def acquire():
        lease = real_lease()
        handles.append(lease)
        acquired.set()
        if not before_transfer:
            assert release.wait(timeout=2)
        return lease

    async def exercise():
        owner = asyncio.create_task(probe_cleanup.acquire_cargo_lease())
        assert await asyncio.to_thread(acquired.wait, 2)
        if before_transfer:
            # The wait wrapper suspends after the worker has returned
            # but before its owner resumes with the handle.
            await ready.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        release.set()

    async def wrapped():
        nonlocal ready
        ready = asyncio.Event()
        if before_transfer:
            wait = asyncio.wait

            async def hold_delivery(tasks, **kwargs):
                await wait(tasks, **kwargs)
                ready.set()
                await asyncio.Future()

            monkeypatch.setattr(probe_cleanup.asyncio, 'wait', hold_delivery)
        await asyncio.wait_for(exercise(), 3)

    ready = None
    monkeypatch.setattr(h, 'cargo_lease', acquire)
    asyncio.run(wrapped())
    assert handles[0].closed
    with real_lease(exclusive=True):
        pass


def test_async_lease_deadline_includes_executor_queueing(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from plangraph.spawner import probe_cleanup

    monkeypatch.setattr(probe_cleanup, 'SHARED_WAIT_S', 0.025)
    monkeypatch.setattr(h, 'SHARED_WAIT_S', 0.025)
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(tmp_path / 'target'))
    release = Event()

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupied = loop.run_in_executor(None, release.wait)
        try:
            with pytest.raises(h.CargoLeaseTimeout) as failure:
                await asyncio.wait_for(probe_cleanup.acquire_cargo_lease(), timeout=0.5)
            assert failure.value.evidence['wait_budget_ms'] == 25
            assert not failure.value.evidence['contention_observed']
        finally:
            release.set()
            await occupied

    asyncio.run(exercise())
    assert not (tmp_path / 'target.plangraph-lock').exists()


def test_late_async_lease_delivery_retains_lock_identity(tmp_path, monkeypatch):
    from plangraph.spawner import probe_cleanup

    target = tmp_path / 'target'
    lock = target.with_suffix('.plangraph-lock')
    wait = asyncio.wait
    now = 100.0

    async def deliver_late(tasks, **kwargs):
        nonlocal now
        result = await wait(tasks, **kwargs)
        now += probe_cleanup.SHARED_WAIT_S + 0.1
        return result

    monkeypatch.setattr(probe_cleanup, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(asyncio, 'wait', deliver_late)
    with pytest.raises(h.CargoLeaseTimeout) as failure:
        asyncio.run(probe_cleanup.acquire_cargo_lease(target))
    evidence = failure.value.evidence
    assert not evidence['contention_observed']
    assert Path(evidence['lock_path']) == lock.resolve()
    if os.name == 'posix':
        stat = lock.stat()
        assert evidence['lock_identity'] == dict(device=stat.st_dev, inode=stat.st_ino)
    assert {'lock_identity', 'lock_path'}.isdisjoint(failure.value.public())
    with h._cargo_lease_for(target, exclusive=True):
        pass
