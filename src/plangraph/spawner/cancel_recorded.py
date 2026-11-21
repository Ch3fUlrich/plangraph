"""Cancel one spawn run; a recorded root is killed only when proven ours (fail closed)."""
import asyncio
import time

from .private_io import SAVE_READ_ATTEMPTS


async def cancel_recorded(service, run_id):
    record = service.result(run_id, attempts=SAVE_READ_ATTEMPTS)
    if record['status'] == 'queued' and not record.get('survivors'):
        # A queued run holds no process: cancelling it ends the wait (its owner sees the status).
        record.update(status='cancelled', reason='cancelled_while_queued', exit_code=1, ended=time.time())
        service._save(record)
        return record
    if record['status'] not in ('running', 'dry_run') and not record.get('survivors'):
        # A finisher may already have published a newer record: never rewrite
        # an unchanged terminal snapshot.
        return record
    if record.get('origin_receipt') or (record.get('run_kind') == 'native-codex' and record.get('overflow_remote')):
        from .remote_ps import native_result
        record = await asyncio.to_thread(native_result, record, verb='cancel')
        service._save(record)
        return record
    if record.get('run_kind') == 'native-codex':
        service._path(run_id).with_name('cancelled').touch()
        until = time.monotonic() + 20
        while time.monotonic() < until:
            await asyncio.sleep(0.05)
            record = service.result(run_id, attempts=SAVE_READ_ATTEMPTS)
            if record['status'] != 'running':
                return record
    survivors = list(record.get('survivors', []))
    if record.get('job_id') in service.runner.jobs:
        await service.runner.cancel(record['job_id'])
        survivors = list(service.runner.jobs[record['job_id']].survivors) or survivors
    elif record['status'] == 'running' and record.get('pid'):
        survivors = await service.runner.kill_recorded(record['pid'], record.get('pid_start'), record.get('job_name'), record.get('escapees', ()))
    task = service.tasks.get(run_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    record.update(status='failed' if survivors else 'cancelled', exit_code=1, ended=time.time())
    if survivors:   # unprovable ownership or survivors: terminal kill_incomplete, never 'cancelled'
        record.update(status='failed', reason='kill_incomplete', survivors=survivors,
                      events=record.get('events', []) + [{'event': 'kill_incomplete', 'survivors': survivors}])
    service._save(record)
    return record
