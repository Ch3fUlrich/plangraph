"""Bounded memory-wait lifecycle and ownership-safe recorded cancellation."""
import asyncio
import errno
import math
import time
from contextlib import asynccontextmanager, ExitStack

from .private_io import SAVE_READ_ATTEMPTS
from .notify import inbox_lock


def queue_now():
    return time.time()

def expire_queue(service, record):
    """Compute an expired view without locking, publishing or changing storage."""
    record = dict(record)
    if record.get('status') in ('queued', 'queue_claimed'):
        record.setdefault('queue_bound_s', 900)
        record.setdefault('queued_until', record.get('started', 0) + record['queue_bound_s'])
        if queue_now() >= record['queued_until']:
            record.update(status='failed', reason='queue_timeout', exit_code=1,
                          ended=queue_now(), error='queue_timeout')
    return record

@asynccontextmanager
async def queue_lock(service, run_id, timeout=1):
    """Bound contention and yield the loop instead of blocking on another process."""
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(inbox_lock(service._path(run_id), blocking=False))
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK): raise
                if time.monotonic() >= deadline: raise TimeoutError('queue lock deadline') from exc
                await asyncio.sleep(.01)
        yield

async def register_execute(service, record, execute, queued):
    if queued:
        async with queue_lock(service, record['run_id']):
            current = service.result(record['run_id'], expire=False)
            if current['status'] != 'queue_claimed': return
            expired = expire_queue(service, current)
            if expired['status'] == 'failed':
                service._save(expired)
                return
            if execute:
                record.update(status='running', started=time.time())
                record.pop('reason', None)
            service._save(record)
            if execute is None: return
            task = service.tasks[record['run_id']] = asyncio.create_task(execute())
            return task
    if execute is None:
        service._save(record)
        return
    task = service.tasks[record['run_id']] = asyncio.create_task(execute())
    return task

async def fail_wait(service, run_id, reason, error):
    async with queue_lock(service, run_id):
        record = service.result(run_id, expire=False)
        if record['status'] in ('queued', 'queue_claimed'):
            record.update(status='failed', reason=reason, error=error, ended=queue_now(), exit_code=1)
            service._save(record)

async def queue_pause(event, delay):
    deadline = time.monotonic() + delay
    token = event.wake_path.stat().st_mtime_ns if event.wake_path.exists() else None
    while not event.is_set() and time.monotonic() < deadline:
        try:
            await asyncio.wait_for(event.wait(), min(.1, max(0, deadline - time.monotonic())))
        except asyncio.TimeoutError:
            pass
        current = event.wake_path.stat().st_mtime_ns if event.wake_path.exists() else None
        if current != token:
            return  # another CLI supervisor published a run exit

def enqueue_memory(service, record, options, card, config):
    """Explicit opt-in only: the accepting service must own a live supervisor loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no owner can drive this wait: retain the immediate refusal
    bound = float(config.get('queue_bound_s', 900))
    if not math.isfinite(bound) or bound <= 0:
        raise ValueError('queue_bound_s must be finite and positive')
    now = queue_now()
    record.update(status='queued', reason='memory_recovery', exit_code=0, ended=None,
                  queued_at=now, queued_until=now + bound, queue_bound_s=bound)
    record.pop('error', None)
    service._queued = getattr(service, '_queued', {})
    event = service._queued[record['run_id']] = asyncio.Event()
    event.wake_path = service.state / 'queue-wake'
    service.tasks[record['run_id']] = loop.create_task(_wait_memory(service, record, options, card, event))

async def _wait_memory(service, record, options, card, event):
    from . import overflow
    run_id = record['run_id']
    try:
        while True:
            async with queue_lock(service, run_id):
                record = service.result(run_id, expire=False)
                if record['status'] != 'queued': return
                expired = expire_queue(service, record)
                if expired['status'] == 'failed':
                    service._save(expired)
                    return
            event.clear()
            now = queue_now()
            role_mb, floor = overflow.local_limits(overflow.registry_caps(), options.get('role', 'implement'))
            memory = overflow.available_memory_mb()
            record.update(mem_available_mb=memory, floor_mb=floor, role_mb=role_mb)
            if not overflow.memory_refusal(memory, role_mb, floor):
                async with queue_lock(service, run_id):
                    record = service.result(run_id, expire=False)
                    if record['status'] != 'queued': return
                    if queue_now() >= record['queued_until']: continue
                    record.update(status='queue_claimed', mem_available_mb=memory, floor_mb=floor, role_mb=role_mb)
                    service._save(record)
                service._queued.pop(run_id, None)
                await service._spawn(**dict(options, card=card, wait_seconds=None, _queued_record=record))
                return
            await queue_pause(event, min(5, record['queued_until'] - now))
    except asyncio.CancelledError:
        await fail_wait(service, run_id, 'queue_interrupted', 'queue_interrupted')
        raise
    except Exception as exc:
        await fail_wait(service, run_id, 'queue_start_failed', str(exc))
    finally:
        service._queued.pop(run_id, None)


async def cancel_recorded(service, run_id):
    async with queue_lock(service, run_id):
        record = service.result(run_id, attempts=SAVE_READ_ATTEMPTS, expire=False)
        queued = record['status'] in ('queued', 'queue_claimed')
        if queued:
            record.update(status='cancelled', reason='cancelled', error=None, exit_code=0, ended=time.time())
            service._save(record)  # durable before the waiter can claim the run
    if not queued and record['status'] not in ('running', 'dry_run') and not record.get('survivors'):
        # A finisher may already have published a newer record: never rewrite
        # an unchanged terminal snapshot.
        return record
    survivors = list(record.get('survivors', []))
    if not queued and record.get('job_id') in service.runner.jobs:
        await service.runner.cancel(record['job_id'])
        survivors = list(service.runner.jobs[record['job_id']].survivors) or survivors
    elif record['status'] == 'running' and record.get('pid'):
        survivors = await service.runner.kill_recorded(record['pid'], record.get('pid_start'), record.get('job_name'), record.get('escapees', ()))
    task = service.tasks.get(run_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if queued:
        getattr(service, '_queued', {}).pop(run_id, None)
        return service.result(run_id, expire=False)  # already durably cancelled under the lock
    record.update(status='failed' if survivors else 'cancelled', exit_code=1, ended=time.time())
    if survivors:   # unprovable ownership or survivors: terminal kill_incomplete, never 'cancelled'
        record.update(status='failed', reason='kill_incomplete', survivors=survivors,
                      events=record.get('events', []) + [{'event': 'kill_incomplete', 'survivors': survivors}])
    service._save(record)
    return record
