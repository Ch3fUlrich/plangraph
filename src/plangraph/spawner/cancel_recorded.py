"""Cancel one spawn run; a recorded root is killed only when proven ours (fail closed)."""
import asyncio
import time

from .private_io import SAVE_READ_ATTEMPTS


async def cancel_recorded(service, run_id):
    record = service.result(run_id, attempts=SAVE_READ_ATTEMPTS)
    if record['status'] not in ('running', 'dry_run') and not record.get('survivors'):
        # A finisher may already have published a newer record: never rewrite
        # an unchanged terminal snapshot.
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
