"""Reap one finished run: kill the run's tree and prove it gone."""
import asyncio
import os

from .proc_kill import KillIncomplete, finish, snapshot


async def reap(runner, job_id, proc):
    job = runner.jobs.get(job_id)
    # F3: kill the run's Job Object first, then pop it — taskkill /T cannot
    # find a root pid that has already exited (SPAWN-ORPHAN-REAP).
    win_job = getattr(runner, '_win_jobs', {}).pop(job_id, None)
    ident = getattr(runner, '_owned', {}).get(job_id, {}).get('pid_start') or (
        'nt' if win_job is not None and os.name == 'nt' else None)   # a Job Object proves ownership
    members, api = {}, getattr(runner, 'win_job_api', None)
    if ident is not None and getattr(proc, 'pid', None) is not None:
        try:   # snapshot BEFORE any signal; FakeWinJob-style stubs have no class-level pids()
            jp = api.pids(win_job) if win_job is not None and hasattr(type(api), 'pids') else None
            members = await asyncio.to_thread(snapshot, proc.pid, ident, job_pids=jp)
        except Exception:
            members = {}
    if win_job is not None:
        try:
            api.terminate(win_job)
        except Exception as exc:
            if job is not None:
                job.events.append({'event': 'reap_failed', 'error': type(exc).__name__})
        finally:
            try:
                api.close(win_job)
            except Exception:
                pass  # handle already invalid; the terminate result is recorded
    pid = getattr(proc, 'pid', None)
    if pid is None:
        return
    try:
        res = runner.tree_killer(pid)
        if asyncio.iscoroutine(res):
            await res
    except ProcessLookupError:
        pass  # the run's process group is already gone
    except Exception as exc:
        # F2: never swallow a mis-kill or platform failure silently; type
        # name only, no message text or pid list. Never raise out of here.
        if job is not None:
            job.events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    try:   # only a tree proven ours is killed further and verified; survivors fail the run
        if ident is not None:
            await finish(pid, ident, members)
    except KillIncomplete as exc:
        if job is not None:
            job.survivors = exc.survivors
            job.status, job.exit_code = 'failed', job.exit_code or 1
            job.events.append({'event': 'kill_incomplete', 'survivors': exc.survivors})
    except Exception as exc:
        if job is not None:
            job.events.append({'event': 'reap_failed', 'error': type(exc).__name__})
