"""Bounded commit-task liveness checks, shared by dispatch, CLI and MCP."""
import asyncio
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from plangraph.routing_support import leg_state

PROMPT = 'Create a file ok.txt containing the single word ok, then git add it and git commit -m probe. Do nothing else.'

#: The probe repo and its child commit ignore the operator's global/system git
#: config (M11): a host commit.gpgsign=true or global core.hooksPath must never
#: turn probe infrastructure into a recorded leg failure.
GIT_ISOLATION = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def seconds(name, default, value=None):
    value = float(os.environ.get(name, default) if value is None else value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('invalid ' + name)
    return value


def key(leg):
    """One leg key for probe and quota bookkeeping (Q16/M8).

    'agy/<model>' only for an agy leg's bare model; a model that already names
    its provider is its own key.  Legs without a harness attribute (legacy
    record rows) key on the model string alone, keeping historical rows stable.
    """
    return ('agy/' + leg.model if getattr(leg, 'harness', None) == 'agy' and '/' not in leg.model
            else leg.model)


def read_status(reader, model, **kw):
    """Read a bare model under both keys: an agy leg books 'agy/<model>', others the bare model (Q16)."""
    from plangraph.routing_support.liveness import unavailable
    state = reader(model, **kw)
    return state if '/' in model or unavailable(state) else reader('agy/' + model, **kw)


def _store_for(runner):
    """The store the planner reads; probe results never land elsewhere (M19)."""
    store = getattr(runner, 'leg_reader', None)
    return store if hasattr(store, 'record_probe_fail') else leg_state


async def _await_completion(runner, job, deadline):
    """Wait out the session even when its task is not registered yet (Q10)."""
    while True:
        task = (getattr(runner, '_tasks', None) or {}).get(job.job_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), max(deadline - time.monotonic(), 0))
            return
        if getattr(job, 'exit_code', None) is not None or getattr(job, 'status', 'running') != 'running':
            return
        if time.monotonic() >= deadline:
            raise asyncio.TimeoutError
        await asyncio.sleep(0.02)


async def probe(runner, leg, *, timeout_s=None, store=None):
    """One real commit task in a disposable repo.

    Probe infrastructure failures (git/gpg/hooks in the temp repo) are reported
    with ``infra=True`` and never recorded as leg failures.
    """
    timeout = seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, timeout_s)
    state = store if store is not None else leg_state
    start = time.monotonic()
    deadline = start + timeout
    root = tempfile.mkdtemp(prefix='plangraph-leg-probe-')
    job = None
    spawn_task = None

    def git(*args):
        return subprocess.check_output(['git', '-C', root, *args], env=dict(os.environ, **GIT_ISOLATION),
                                       stderr=subprocess.DEVNULL).decode().strip()

    try:
        ok = reason = None
        infra = False
        try:
            hooks = Path(root) / 'probe-hooks'
            hooks.mkdir()
            await asyncio.to_thread(git, 'init')
            await asyncio.to_thread(git, 'config', 'user.name', 'Plangraph probe')
            await asyncio.to_thread(git, 'config', 'user.email', 'probe@plangraph.invalid')
            # Repo-local, not only on this commit: the writer's own commit must
            # also survive a hostile global gpgsign/hooksPath (M11).
            await asyncio.to_thread(git, 'config', 'commit.gpgsign', 'false')
            await asyncio.to_thread(git, 'config', 'core.hooksPath', str(hooks))
            await asyncio.to_thread(git, 'commit', '--allow-empty', '-m', 'initial')
            before = await asyncio.to_thread(git, 'rev-parse', 'HEAD')
        except (OSError, subprocess.CalledProcessError):
            ok, reason, infra = False, 'probe infra: git', True
        if ok is None:
            async def launch():
                return await runner.spawn(prompt=PROMPT, role='implement', model_or_combo=leg.model,
                                         client=leg.harness, cwd=root, extra_env=GIT_ISOLATION,
                                         watchdog=dict(stall_s=timeout + 60, first_edit_s=timeout + 60,
                                                       max_s=timeout + 60, interval=1))
            spawn_task = asyncio.ensure_future(launch())
            try:
                job = await asyncio.wait_for(asyncio.shield(spawn_task), max(deadline - time.monotonic(), 0))
            except asyncio.TimeoutError:
                ok, reason = False, 'timeout'
            except (ValueError, RuntimeError, OSError):
                ok, reason = False, 'exit 1'
        if ok is None:
            try:
                await _await_completion(runner, job, deadline)
            except asyncio.TimeoutError:
                ok, reason = False, 'timeout'
        if ok is None:
            try:
                changed = before != await asyncio.to_thread(git, 'rev-parse', 'HEAD')
                files = (await asyncio.to_thread(git, 'ls-tree', '--name-only', 'HEAD')).splitlines()
            except (OSError, subprocess.CalledProcessError):
                ok, reason, infra = False, 'probe infra: git', True
            else:
                ok = changed and 'ok.txt' in files
                reason = '' if ok else f'exit {job.exit_code}' if job.exit_code else 'no commit'
        if not infra:
            if ok:
                state.record_probe_pass(key(leg), caller='leg-probe')
            else:
                state.record_probe_fail(key(leg), reason, caller='leg-probe')
        return dict(ok=ok, reason=reason, infra=infra, seconds=time.monotonic() - start)
    finally:
        if spawn_task is not None and job is None:
            # The budget may expire mid-spawn (M10): let spawn finish so the
            # registered child is cancelled instead of orphaned on rmtree.
            try:
                job = await asyncio.wait_for(asyncio.shield(spawn_task), timeout + 5)
            except Exception:
                job = None
        if job is not None:
            task = (getattr(runner, '_tasks', None) or {}).get(job.job_id)
            if task is not None and not task.done():
                await runner.cancel(job.job_id)
                await asyncio.gather(task, return_exceptions=True)
            elif getattr(job, 'status', 'running') == 'running':
                await runner.cancel(job.job_id)
        shutil.rmtree(root, ignore_errors=True)


_INFLIGHT = {}


async def before_launch(runner, leg, role, dry_run):
    ttl = seconds('PLANGRAPH_LEG_PROBE_TTL_S', 1800)
    if not ttl or dry_run or role not in ('implement', 'fix'):
        return None
    state = _store_for(runner)
    ts = state.last_probe_ts(key(leg))  # successes only: a failed probe is not fresh evidence
    if ts is not None and time.time() - ts < ttl:
        return None
    # Single flight per leg: concurrent launches share one in-flight probe and
    # its verdict, fresh failure included, instead of each paying a model run.
    pending = _INFLIGHT.get(key(leg))
    if pending is None:
        pending = _INFLIGHT[key(leg)] = asyncio.ensure_future(probe(runner, leg, store=state))
        pending.add_done_callback(lambda _: _INFLIGHT.pop(key(leg), None))
    result = await asyncio.shield(pending)
    return None if result['ok'] or result.get('infra') else 'down: ' + result['reason']


async def probe_many(runner, legs=(), all_writers=False, timeout=None):
    from plangraph.planner.legs import DEFAULT_LEGS
    ids = {legs} if isinstance(legs, str) else set(legs or ())
    if not (ids or all_writers):
        raise ValueError('select --leg ID or --all-writers')
    unknown = ids - {leg.id for leg in DEFAULT_LEGS}
    if unknown:
        raise ValueError('unknown legs: ' + ', '.join(sorted(unknown)))
    selected = [leg for leg in DEFAULT_LEGS if leg.id in ids or
                (all_writers and any(role in leg.roles for role in ('implement', 'fix')))]
    results = []
    for leg in selected:
        results.append(dict(id=leg.id, **await probe(runner, leg, timeout_s=timeout)))
    return dict(ok=all(row['ok'] for row in results), probes=results)
