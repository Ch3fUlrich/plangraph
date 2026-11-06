"""Keep disposable probe ownership until launch and verified cancellation finish."""
import asyncio
import shutil
from contextvars import ContextVar

OWNED = ContextVar('probe_owned', default=None)
WAIT_S = 5


def register(job_id):
    ids = OWNED.get()
    if ids is not None:
        ids.add(job_id)


async def bounded(awaitable):
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait([task], timeout=WAIT_S)
    if not done:
        task.cancel()
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        raise TimeoutError('probe cleanup bounded wait expired; directory retained')
    return task.result()


async def cleanup(runner, spawn_task, job, root, owned_ids=()):
    errors = []
    safe = True
    try:
        if spawn_task is not None and job is None:
            # Do not abandon a shielded spawn: it may already own a process.
            try:
                job = await bounded(spawn_task)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, TimeoutError):
                    errors.append(exc)
                    safe = False
        jobs = [job] if job is not None else []
        # spawn can register a child and then raise before returning its job.
        jobs += [j for jid, j in getattr(runner, 'jobs', {}).items()
                 if jid in owned_ids and j not in jobs]
        for owned in jobs:
            try:
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
