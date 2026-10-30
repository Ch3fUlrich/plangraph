"""Bounded commit-task liveness checks, shared by dispatch, CLI and MCP."""
import asyncio
import math
import os
import shutil
import subprocess
import tempfile
import time

from plangraph.routing_support import leg_state

PROMPT = 'Create a file ok.txt containing the single word ok, then git add it and git commit -m probe. Do nothing else.'


def seconds(name, default, value=None):
    value = float(os.environ.get(name, default) if value is None else value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('invalid ' + name)
    return value


def key(leg):
    return 'agy/' + leg.model if leg.harness == 'agy' and '/' not in leg.model else leg.model


async def probe(runner, leg, *, timeout_s=None):
    timeout = seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, timeout_s)
    start = time.monotonic()
    root = tempfile.mkdtemp(prefix='plangraph-leg-probe-')
    job = None
    def git(*args):
        return subprocess.check_output(['git', '-C', root, *args], stderr=subprocess.DEVNULL).decode().strip()
    try:
        await asyncio.to_thread(git, 'init')
        await asyncio.to_thread(git, 'config', 'user.name', 'Plangraph probe')
        await asyncio.to_thread(git, 'config', 'user.email', 'probe@plangraph.invalid')
        await asyncio.to_thread(git, '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'initial')
        before = await asyncio.to_thread(git, 'rev-parse', 'HEAD')
        async def run():
            nonlocal job
            job = await runner.spawn(prompt=PROMPT, role='implement', model_or_combo=leg.model,
                                     client=leg.harness, cwd=root,
                                     watchdog=dict(stall_s=timeout + 60, first_edit_s=timeout + 60,
                                                   max_s=timeout + 60, interval=1))
            task = runner._tasks.get(job.job_id)
            if task:
                await asyncio.shield(task)
        try:
            await asyncio.wait_for(run(), timeout)
            changed = before != await asyncio.to_thread(git, 'rev-parse', 'HEAD')
            files = (await asyncio.to_thread(git, 'ls-tree', '--name-only', 'HEAD')).splitlines()
            ok = changed and 'ok.txt' in files
            reason = '' if ok else f'exit {job.exit_code}' if job.exit_code else 'no commit'
        except asyncio.TimeoutError:
            ok, reason = False, 'timeout'
        except (OSError, ValueError, RuntimeError):
            ok, reason = False, 'exit 1'
        if ok:
            leg_state.record_probe_pass(key(leg), caller='leg-probe')
        else:
            leg_state.record_probe_fail(key(leg), reason, caller='leg-probe')
        return dict(ok=ok, reason=reason, seconds=time.monotonic() - start)
    finally:
        if job is not None:
            task = runner._tasks.get(job.job_id)
            if task and not task.done():
                await runner.cancel(job.job_id)
                await asyncio.gather(task, return_exceptions=True)
        shutil.rmtree(root, ignore_errors=True)


async def before_launch(runner, leg, role, dry_run):
    ttl = seconds('PLANGRAPH_LEG_PROBE_TTL_S', 1800)
    if not ttl or dry_run or role not in ('implement', 'fix'):
        return None
    ts = leg_state.last_probe_ts(key(leg))
    if ts is not None and time.time() - ts < ttl:
        return None
    result = await probe(runner, leg)
    return None if result['ok'] else 'down: ' + result['reason']


async def probe_many(runner, legs=(), all_writers=False, timeout=None):
    from plangraph.planner.legs import DEFAULT_LEGS
    ids = set(legs)
    unknown = ids - {leg.id for leg in DEFAULT_LEGS}
    if unknown or not (ids or all_writers):
        raise ValueError('select --leg ID or --all-writers; unknown legs: ' + ', '.join(sorted(unknown)))
    selected = [leg for leg in DEFAULT_LEGS if leg.id in ids or
                (all_writers and any(role in leg.roles for role in ('implement', 'fix')))]
    results = []
    for leg in selected:
        results.append(dict(id=leg.id, **await probe(runner, leg, timeout_s=timeout)))
    return dict(ok=all(row['ok'] for row in results), probes=results)
