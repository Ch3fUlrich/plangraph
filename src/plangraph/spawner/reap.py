"""Reap one finished run: kill and prove gone only a tree proven ours (fail closed)."""
import asyncio

from . import proc_kill
from .proc_kill import KillIncomplete, alive, finish, group_members, kill_process_tree, snapshot, verify_gone

SAMPLE_S = 0.25


async def cancel_and_close(task, proc):
    """Cancel ``task`` and close the child's pipe transports only (never signals the process)."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    tr = getattr(proc, '_transport', None)
    for fd in (0, 1, 2):
        try:
            pipe = tr.get_pipe_transport(fd) if tr is not None else None
            if pipe is not None:
                pipe.close()
        except Exception:
            pass


async def sample_tree(seen, pid, ident):
    """Accumulate the run's (pid, starttime) tree while it runs, so a setsid() descendant
    orphaned after its parent exits is still known at reap (bounded: a process that lives
    <SAMPLE_S is missed; a subreaper or cgroup would be needed to close that fully)."""
    while True:
        try:
            seen.update(await asyncio.to_thread(snapshot, pid, ident))
        except Exception:
            pass
        await asyncio.sleep(SAMPLE_S)


async def kill_proven(runner, pid, ident, *, plat, win_job=None, seen=None, esc=None, events=None):
    """Kill and prove gone the tree of ``pid``, owned via ``ident``.  Returns survivors, or None when clean."""
    events = events if events is not None else []
    incomplete = None
    members, api = dict(seen or {}), getattr(runner, 'win_job_api', None)
    live = (lambda: api.pids(win_job)) if win_job is not None and hasattr(type(api), 'pids') else None
    try:   # snapshot BEFORE any signal
        members.update(await asyncio.to_thread(snapshot, pid, ident, platform=plat,
                                               job_pids=live() if live else None))
    except KillIncomplete as exc:
        incomplete = exc.survivors
    except Exception:
        incomplete = [pid]
    if win_job is not None:
        try:
            api.terminate(win_job)
        except Exception as exc:
            events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    if incomplete is None and esc:   # nt: descendants that predate job assignment
        try:
            table = await asyncio.to_thread(proc_kill._nt_table)
            todo = {p: None for p, pp in esc.items() if table.get(p) == pp}
            if todo:
                await kill_process_tree(pid, platform='nt', members=todo)
                await verify_gone(todo, platform='nt')
        except KillIncomplete as exc:
            incomplete = exc.survivors
        except Exception as exc:
            incomplete = sorted(esc)
            events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    if incomplete is None:
        try:
            if getattr(runner.tree_killer, '__func__', None) is type(runner)._own_kill:
                res = runner.tree_killer(pid, ident=ident)
            else:
                res = runner.tree_killer(pid)
            if asyncio.iscoroutine(res):
                await res
        except ProcessLookupError:
            pass  # the run's process group is already gone
        except KillIncomplete as exc:
            incomplete = exc.survivors
        except Exception as exc:
            events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    if incomplete is None:   # only a tree proven ours is verified; survivors fail the run
        try:
            await finish(pid, ident, members, platform=plat, live_pids=live,
                         reterminate=(lambda: api.terminate(win_job)) if live else None)
        except KillIncomplete as exc:
            incomplete = exc.survivors
        except Exception as exc:
            incomplete = [pid]
            events.append({'event': 'reap_failed', 'error': type(exc).__name__})
    if win_job is not None:
        try:
            api.close(win_job)
        except Exception:
            pass  # handle already invalid; the terminate result is recorded
    return incomplete


def unproven_survivors(pid, plat):
    """Ownership unproven: nothing is signalled; what is still alive is only reported."""
    out = [pid] if alive(pid, None, platform=plat) else []
    if plat != 'nt':
        out = sorted(set(out) | set(group_members(pid)))   # the group id outlives a dead root
    return out


async def reap(runner, job_id, proc):
    job = runner.jobs.get(job_id)
    win_job = getattr(runner, '_win_jobs', {}).pop(job_id, None)
    owned = getattr(runner, '_owned', {}).pop(job_id, {})   # pruned on reap: never matched again
    sampler = getattr(runner, '_samplers', {}).pop(job_id, None)
    if sampler is not None:
        sampler.cancel()
    seen = getattr(runner, '_seen', {}).pop(job_id, {})
    esc = getattr(runner, '_escapees', {}).pop(job_id, None)
    pid = getattr(proc, 'pid', None)
    if pid is None:
        return   # pid-less process objects (test fakes): nothing of ours to track or kill
    plat = 'nt' if runner._nt() else None
    ident = owned.get('pid_start') or ('nt' if win_job is not None and plat == 'nt' else None)   # a Job Object proves ownership
    events = job.events if job is not None else []
    if ident is None:   # ownership unproven: no kill path runs; any survivor is terminal kill_incomplete
        incomplete = unproven_survivors(pid, plat) or None
    else:
        incomplete = await kill_proven(runner, pid, ident, plat=plat, win_job=win_job, seen=seen,
                                       esc=esc, events=events)
    if incomplete is not None and job is not None:
        job.survivors = list(incomplete) or [pid]
        job.status, job.exit_code = 'failed', job.exit_code or 1
        job.events.append({'event': 'kill_incomplete', 'survivors': job.survivors})
