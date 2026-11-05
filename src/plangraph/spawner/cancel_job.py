"""Publish cancellation only after tree cleanup has completed."""
import asyncio
import time


def publish_cancel(job):
    if job.survivors:
        job.status, job.exit_code = 'failed', job.exit_code or 1
        if not any(e['event'] == 'kill_incomplete' for e in job.events):
            job.events.append(dict(event='kill_incomplete', survivors=list(job.survivors)))
    elif job.status in ('running', 'dry_run'):
        job.status, job.exit_code = 'cancelled', 1


async def cancel_job(runner, job_id):
    if not isinstance(job_id, str):
        from plangraph.routing_errors import ParameterRefused
        raise ParameterRefused('job_id must be a string')
    if job_id not in runner.jobs:
        raise KeyError(f'Job {job_id} not found')
    job = runner.jobs[job_id]
    # Stop the monitor first: its finally block owns reap and verification.
    task = runner._tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    proc = runner._processes.get(job_id)
    if proc:
        await runner._reap_process(job_id, proc)
    publish_cancel(job)
    runner._nonces.pop(job_id, None)
    job.end_time = time.time()
    runner._check_git_config_guard(job_id)
    return dict(job_id=job_id, status=job.status, survivors=list(job.survivors))
