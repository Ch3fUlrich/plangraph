"""Keep disposable probe ownership until launch and verified cancellation finish."""
import asyncio
import shutil


async def cleanup(runner, spawn_task, job, root):
    try:
        if spawn_task is not None and job is None:
            # Do not abandon a shielded spawn: it may already own a process.
            try:
                job = await spawn_task
            except (Exception, asyncio.CancelledError):
                pass
        jobs = [job] if job is not None else []
        # spawn can register a child and then raise before returning its job.
        jobs += [j for j in getattr(runner, 'jobs', {}).values()
                 if getattr(j, 'cwd', None) == root and j not in jobs]
        for owned in jobs:
            await runner.cancel(owned.job_id)
            task = (getattr(runner, '_tasks', None) or {}).get(owned.job_id)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
