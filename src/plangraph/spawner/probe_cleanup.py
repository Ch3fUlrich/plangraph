"""Keep disposable probe ownership until launch and verified cancellation finish."""
import asyncio
import shutil
from threading import Event, Lock
import time
from contextvars import ContextVar

OWNED = ContextVar('probe_owned', default=None)
LEASE_REQUEST = ContextVar('cargo_lease_request', default=None)
WAIT_S = 5
SHARED_WAIT_S = 5.0


class LocalLaunchFault(OSError):
    """A launch that stopped locally before process creation."""

    def __init__(self, code, **evidence):
        super().__init__(code)
        self.code = code
        self.evidence = dict(code=code, **evidence)

    def public(self):
        return {key: value for key, value in self.evidence.items()
                if key not in ('lock_path', 'lock_identity')}


class LeaseWait:
    """One acquisition request, timed before it enters the executor."""

    def __init__(self, wait, deadline=None, stop=None, target=None):
        self.started = time.monotonic()
        self.deadline = deadline
        self.stop = stop if stop is not None else Event()
        self.target = target
        self.lock_path = None
        self.conflict_identity = None
        self.finished = None
        self._lock = Lock()
        self._lease = None
        self.limit(wait)

    def limit(self, wait):
        self.immediate = wait == 0
        self.end = self.started + wait if self.deadline is None else min(self.started + wait, self.deadline)

    def expired(self, immediate=False):
        now = time.monotonic()
        return (self.stop.is_set() or (self.deadline is not None and now >= self.deadline)
                or (not immediate and now >= self.end))

    def evidence(self, identity=None):
        identity = self.conflict_identity or identity or 'unavailable'
        end = self.finished if self.finished is not None else time.monotonic()
        return dict(waited_ms=round(max(0, end - self.started) * 1000),
                    wait_budget_ms=round(max(0, self.end - self.started) * 1000),
                    contention_observed=self.conflict_identity is not None,
                    lock_path=str(self.lock_path) if self.lock_path is not None else 'unavailable',
                    lock_identity=identity, lock_identity_available=identity != 'unavailable')

    def hold(self, lease):
        with self._lock:
            if not self.stop.is_set():
                self._lease = lease
                return
        if lease is not None:
            lease.close()

    def take(self):
        with self._lock:
            lease, self._lease = self._lease, None
            return lease


async def acquire_cargo_lease(target=None):
    """Transfer the worker's handle once, or close it when delivery is abandoned."""
    from .host_ready import CargoLeaseTimeout, cargo_lease
    launch = OWNED.get()
    request = LeaseWait(SHARED_WAIT_S, launch.deadline if launch else None,
                        launch.stop if launch else None, target)
    if launch:
        launch.wait = request
        launch.stage = 'cargo_lease'

    def acquire():
        request.hold(cargo_lease())

    token = LEASE_REQUEST.set(request)
    try:
        worker = asyncio.create_task(asyncio.to_thread(acquire))
    finally:
        LEASE_REQUEST.reset(token)
    try:
        done, _ = await asyncio.wait([worker], timeout=max(0, request.end - time.monotonic()))
        if not done:
            raise CargoLeaseTimeout(request)
        worker.result()
        if request.expired(immediate=request.immediate):
            raise CargoLeaseTimeout(request)
    except (asyncio.CancelledError, CargoLeaseTimeout):
        request.stop.set()
        if (lease := request.take()) is not None:
            lease.close()
        worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        raise
    request.finished = time.monotonic()
    if launch:
        launch.stage = 'pre_launch'
    return request.take()


class ProbeLaunch:
    """Own the deadline, launch stage and jobs of one disposable repo."""

    def __init__(self, started, deadline):
        self.started, self.deadline = started, deadline
        self.stop = Event()
        self.ids = set()
        self.wait = None
        self.stage = 'pre_launch'
        self.dispatched = False
        self.cancelled = False
        self.fault = None

    def cancel(self):
        self.cancelled = True
        self.stop.set()

    def expire(self):
        self.stop.set()
        if self.fault is None:
            evidence = (self.wait.evidence() if self.wait else
                        dict(waited_ms=round((time.monotonic() - self.started) * 1000),
                             wait_budget_ms=round((self.deadline - self.started) * 1000),
                             contention_observed=False, lock_identity_available=False))
            self.fault = LocalLaunchFault('probe_pre_dispatch_timeout', stage=self.stage, **evidence)
        return self.fault


def gate_dispatch(*, enter=False):
    launch = OWNED.get()
    if launch is not None:
        if launch.cancelled:
            raise asyncio.CancelledError
        if launch.fault:
            raise launch.fault
        if time.monotonic() >= launch.deadline:
            raise launch.expire()
        if enter:
            launch.dispatched = True


def register(job_id):
    launch = OWNED.get()
    if launch is not None:
        launch.ids.add(job_id)


def fail_local_job(job, fault):
    launch = OWNED.get()
    if launch is not None:
        if launch.cancelled:
            raise asyncio.CancelledError
        fault = launch.fault or fault
    job.status, job.exit_code, job.end_time = 'failed', 1, time.time()
    job._local_launch_fault = fault
    job.events.append(dict(event=fault.code, local_fault=fault.public()))
    return job


class CleanupTimeout(TimeoutError):
    pass


async def bounded(awaitable):
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait([task], timeout=WAIT_S)
    if not done:
        task.cancel()
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        raise CleanupTimeout('probe cleanup bounded wait expired; directory retained')
    return task.result()


async def cleanup(runner, spawn_task, job, root, owned_ids=(), *, fault=None):
    errors = []
    safe = True
    try:
        if spawn_task is not None and job is None:
            # Do not abandon a shielded spawn: it may already own a process.
            try:
                job = await bounded(spawn_task)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, CleanupTimeout):
                    errors.append(exc)
                    safe = False
        jobs = [job] if job is not None else []
        # spawn can register a child and then raise before returning its job.
        jobs += [j for jid, j in getattr(runner, 'jobs', {}).items()
                 if jid in owned_ids and j not in jobs]
        for owned in jobs:
            try:
                if fault and not getattr(owned, '_local_launch_fault', None):
                    fail_local_job(owned, fault)
                await bounded(runner.cancel(owned.job_id))
                task = (getattr(runner, '_tasks', None) or {}).get(owned.job_id)
                if task is not None:
                    await bounded(asyncio.gather(task, return_exceptions=True))
                if getattr(owned, 'survivors', []):
                    raise RuntimeError('probe cleanup kill_incomplete; directory retained')
            except (Exception, asyncio.CancelledError) as exc:
                errors.append(exc)
                safe = False
    finally:
        if safe:
            shutil.rmtree(root, ignore_errors=True)
    if errors:
        raise RuntimeError('probe cleanup incomplete; directory retained') from errors[0]
