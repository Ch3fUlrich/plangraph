"""Cancel one spawn run; a recorded root is killed only when proven ours (fail closed)."""
import asyncio
import errno
import math
import time
from contextlib import asynccontextmanager

from .private_io import SAVE_READ_ATTEMPTS
from .notify import inbox_lock
def queue_now(): return time.time()
def expire_queue(service, record):
    record = dict(record)
    if record.get('status') in ('queued', 'queue_claimed'):
        record.setdefault('queued_until', record.get('started', 0) + record.get('queue_bound_s', 900))
        if queue_now() >= record['queued_until']:
            record.update(status='failed', reason='queue_timeout', error='queue_timeout', exit_code=1, ended=queue_now())
    return record
@asynccontextmanager
async def queue_lock(service, run_id, timeout=1):
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock = inbox_lock(service._path(run_id), blocking=False)
            lock.__enter__()
            break
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK): raise
            if time.monotonic() >= deadline: raise TimeoutError('queue lock deadline') from exc
            await asyncio.sleep(.01)
    try: yield
    finally: lock.__exit__(None, None, None)
async def register_execute(service, record, execute, queued):
    if queued:
        async with queue_lock(service, record['run_id']):
            current = service.result(record['run_id'], expire=False)
            if current['status'] != 'queue_claimed': return
            expired = expire_queue(service, current)
            if expired['status'] == 'failed': service._save(expired); return
            record.pop('reason', None)
            if execute is None: record['reason'] = record.get('error') or 'queue_start_failed'
            service._save(record)
    elif execute is None: service._save(record)
    if execute:
        service.tasks[record['run_id']] = task = asyncio.create_task(execute())
        return task
async def fail_wait(service, run_id, reason, error):
    while True:
        try:
            async with queue_lock(service, run_id):
                record = service.result(run_id, expire=False)
                if record['status'] in ('queued', 'queue_claimed'):
                    record.update(status='failed', reason=reason, error=error, ended=queue_now(), exit_code=1)
                    service._save(record)
            return
        except (OSError, ValueError, TypeError): await asyncio.sleep(.1)
async def queue_pause(event, delay):
    deadline = time.monotonic() + max(0, delay)
    token = event.wake_path.stat().st_mtime_ns if event.wake_path.exists() else None
    while not event.is_set() and time.monotonic() < deadline:
        try: await asyncio.wait_for(event.wait(), min(.1, max(0, deadline - time.monotonic())))
        except asyncio.TimeoutError: pass
        if (event.wake_path.stat().st_mtime_ns if event.wake_path.exists() else None) != token: return
def enqueue_memory(service, record, options, card, config, *, defer=False):
    try:
        loop = None if defer else asyncio.get_running_loop()
        bound = float(config.get('queue_bound_s', 900))
        if not math.isfinite(bound) or bound <= 0: return
    except (RuntimeError, ValueError, TypeError, OverflowError): return
    record.update(status='queued', reason='memory_recovery', queue_reason='memory_recovery', exit_code=0,
                  ended=None, queued_until=queue_now() + bound, queue_bound_s=bound)
    record.pop('error', None)
    if loop is not None: start_memory_queue(service, record, options, card)
def start_memory_queue(service, record, options, card):
    loop = asyncio.get_running_loop()
    if not hasattr(service, '_queued'): service._queued = {}
    event = service._queued[record['run_id']] = asyncio.Event(); event.wake_path = service.state / 'queue-wake'
    service.tasks[record['run_id']] = loop.create_task(_wait_memory(service, record, options, card, event))
async def _wait_memory(service, record, options, card, event):
    from . import overflow
    run_id = record['run_id']
    try:
        while True:
            try:
                async with queue_lock(service, run_id):
                    record = service.result(run_id, expire=False)
                    if record['status'] != 'queued': return
                    if queue_now() >= record['queued_until']:
                        service._save(expire_queue(service, record)); return
                event.clear()
                role_mb, floor = overflow.local_limits(overflow.registry_caps(), options.get('role', 'implement'))
                memory = overflow.available_memory_mb()
                if not overflow.memory_refusal(memory, role_mb, floor):
                    async with queue_lock(service, run_id):
                        record = service.result(run_id, expire=False)
                        if record['status'] != 'queued': return
                        if queue_now() >= record['queued_until']: continue
                        record.update(status='queue_claimed', mem_available_mb=memory, floor_mb=floor, role_mb=role_mb)
                        service._save(record)
                    break
                await queue_pause(event, min(5, max(0, record['queued_until'] - queue_now())))
            except (OSError, ValueError, TypeError): await asyncio.sleep(.1)  # retain ownership through transient reads/writes
        await service._spawn(**dict(options, card=card, wait_seconds=None, _queued_record=record))
    except asyncio.CancelledError:
        await fail_wait(service, run_id, 'queue_interrupted', 'queue_interrupted')
        raise
    except Exception as exc:
        from plangraph.routing_errors import SpawnRefused
        await fail_wait(service, run_id, str(exc) if isinstance(exc, SpawnRefused) else 'queue_start_failed', str(exc))
    finally: service._queued.pop(run_id, None)


async def cancel_recorded(service, run_id):
    record = service.result(run_id, attempts=SAVE_READ_ATTEMPTS, expire=False)
    queued = False
    if record['status'] in ('queued', 'queue_claimed'):
        async with queue_lock(service, run_id):
            record = service.result(run_id, expire=False)
            queued = record['status'] in ('queued', 'queue_claimed')
            if queued:
                record.update(status='cancelled', reason='cancelled', error=None, exit_code=0, ended=time.time())
                service._save(record)
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
        return service.result(run_id, expire=False)
    record.update(status='failed' if survivors else 'cancelled', exit_code=1, ended=time.time())
    if survivors:   # unprovable ownership or survivors: terminal kill_incomplete, never 'cancelled'
        record.update(status='failed', reason='kill_incomplete', survivors=survivors,
                      events=record.get('events', []) + [{'event': 'kill_incomplete', 'survivors': survivors}])
    service._save(record)
    return record
