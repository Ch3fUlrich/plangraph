"""Reap one finished run: kill and prove gone only a tree proven ours (fail closed)."""
import asyncio

from .proc_kill import KillIncomplete, alive, finish, snapshot


async def reap(runner, job_id, proc):
    job = runner.jobs.get(job_id)
    win_job = getattr(runner, '_win_jobs', {}).pop(job_id, None)
    pid = getattr(proc, 'pid', None)
    if pid is None:
        return   # pid-less process objects (test fakes): nothing of ours to track or kill
    plat = 'nt' if runner._nt() else None
    ident = getattr(runner, '_owned', {}).get(job_id, {}).get('pid_start') or (
        'nt' if win_job is not None and plat == 'nt' else None)   # a Job Object proves ownership
    incomplete = None

    def mark(survivors):
        if job is not None:
            job.survivors = list(survivors) or [pid]
            job.status, job.exit_code = 'failed', job.exit_code or 1
            job.events.append({'event': 'kill_incomplete', 'survivors': job.survivors})
    if ident is None:   # ownership unproven: no kill path runs; a live unproven root is terminal kill_incomplete
        if alive(pid, None, platform=plat):
            mark([pid])
        return
    members, api = {}, getattr(runner, 'win_job_api', None)
    live = (lambda: api.pids(win_job)) if win_job is not None and hasattr(type(api), 'pids') else None
    try:   # snapshot BEFORE any signal
        members = await asyncio.to_thread(snapshot, pid, ident, platform=plat,
                                          job_pids=live() if live else None)
    except KillIncomplete as exc:
        incomplete = exc.survivors
    except Exception:
        incomplete = [pid]
    if win_job is not None:
        try:
            api.terminate(win_job)
        except Exception as exc:
            if job is not None:
                job.events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    if incomplete is None:
        try:
            res = runner.tree_killer(pid)
            if asyncio.iscoroutine(res):
                await res
        except ProcessLookupError:
            pass  # the run's process group is already gone
        except KillIncomplete as exc:
            incomplete = exc.survivors
        except Exception as exc:
            if job is not None:
                job.events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    if incomplete is None:   # only a tree proven ours is verified; survivors fail the run
        try:
            await finish(pid, ident, members, platform=plat, live_pids=live,
                         reterminate=(lambda: api.terminate(win_job)) if live else None)
        except KillIncomplete as exc:
            incomplete = exc.survivors
        except Exception as exc:
            incomplete = [pid]
            if job is not None:
                job.events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    if win_job is not None:
        try:
            api.close(win_job)
        except Exception:
            pass  # handle already invalid; the terminate result is recorded
    if incomplete is not None:
        mark(incomplete)
