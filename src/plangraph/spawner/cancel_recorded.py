"""Bounded memory-wait lifecycle and ownership-safe recorded cancellation."""
import asyncio
import math
import time

from .private_io import SAVE_READ_ATTEMPTS
from .notify import inbox_lock


def queue_now():
    return time.time()

def expire_queue(service, record):
    """Enforce durable bounds on reads too, including after supervisor loss."""
    if record.get('status') == 'queued':
        with inbox_lock(service._path(record['run_id'])):
            record = service.result(record['run_id'], expire=False)
            if record.get('status') == 'queued':
                record.setdefault('queue_bound_s', 900)
                record.setdefault('queued_until', record.get('started', 0) + record['queue_bound_s'])
                if queue_now() >= record['queued_until']:
                    record.update(status='failed', reason='queue_timeout', exit_code=1,
                                  ended=queue_now(), error='queue_timeout')
                    service._save(record)
    return record

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
        while service.result(run_id)['status'] == 'queued':
            event.clear()
            now = queue_now()
            role_mb, floor = overflow.local_limits(overflow.registry_caps(), options.get('role', 'implement'))
            memory = overflow.available_memory_mb()
            record.update(mem_available_mb=memory, floor_mb=floor, role_mb=role_mb)
            if not overflow.memory_refusal(memory, role_mb, floor):
                with inbox_lock(service._path(run_id)):
                    if service.result(run_id, expire=False)['status'] != 'queued': return
                    if queue_now() >= record['queued_until']: continue
                    record.update(status='running', started=now, exit_code=None)
                    record.pop('reason', None)
                    service._save(record)
                service._queued.pop(run_id, None)
                await service._spawn(**dict(options, card=card, wait_seconds=None, _queued_record=record))
                return
            await queue_pause(event, min(5, record['queued_until'] - now))
    except asyncio.CancelledError:
        if service.result(run_id)['status'] == 'queued':
            record.update(status='failed', reason='queue_interrupted', error='queue_interrupted',
                          ended=queue_now(), exit_code=1)
            service._save(record)
        raise
    except Exception as exc:
        record.update(status='failed', reason='queue_start_failed', error=type(exc).__name__,
                      ended=queue_now(), exit_code=1)
        service._save(record)
    finally:
        service._queued.pop(run_id, None)


async def cancel_recorded(service, run_id):
    with inbox_lock(service._path(run_id)):
        record = service.result(run_id, attempts=SAVE_READ_ATTEMPTS, expire=False)
        queued = record['status'] == 'queued'
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
    record.update(status='failed' if survivors else 'cancelled', exit_code=0 if queued else 1, ended=time.time())
    if queued: getattr(service, '_queued', {}).pop(run_id, None)
    if survivors:   # unprovable ownership or survivors: terminal kill_incomplete, never 'cancelled'
        record.update(status='failed', reason='kill_incomplete', survivors=survivors,
                      events=record.get('events', []) + [{'event': 'kill_incomplete', 'survivors': survivors}])
    service._save(record)
    return record
