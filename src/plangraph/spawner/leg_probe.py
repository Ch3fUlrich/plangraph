"""Bounded commit-task liveness checks, shared by dispatch, CLI and MCP."""
import asyncio
import json
import logging
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import time

from plangraph.routing_support import leg_state
from plangraph.routing_errors import ParameterRefused

PROMPT = 'Create a file ok.txt containing the single word ok, then git add it and git commit -m probe. Do nothing else.'

#: The probe repo and its child commit ignore the operator's global/system git
#: config (M11): a host commit.gpgsign=true or global core.hooksPath must never
#: turn probe infrastructure into a recorded leg failure.
GIT_ISOLATION = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}

#: GIT_DIR and kin name the repo every ``git`` resolves regardless of the
#: process cwd. Inherited from a caller inside a lane worktree or a git hook,
#: they redirected the writer's ``git add/commit`` -- and the probe's own git
#: calls -- into the CALLER's worktree (D-3465: probe commits in 3 lanes).
#: GIT_ISOLATION only sets GIT_CONFIG_*; the redirection vars must be *removed*
#: from the probe env, never inherited.
GIT_REDIRECT_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                     'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY')

#: extra_env for the writer child: the same isolation as :func:`probe_env`,
#: expressed as removals -- ``build_child_env`` drops a key whose extra_env
#: value is None. Every harness child inherits this (D-3465).
CHILD_ISOLATION = {**GIT_ISOLATION, **dict.fromkeys(GIT_REDIRECT_VARS)}
LIVE_RUN_WRITE_WINDOW_S = 120


def _run_logs(runner):
    """Read stable running records/log identities only from the owning service."""
    root = getattr(runner, 'state', None)
    snapshots = {}
    if root is None:
        return snapshots
    try:
        paths = list((Path(root) / 'runs').glob('run-*/record.json'))
    except OSError:
        return snapshots
    for path in paths:
        try:
            text = path.read_text(encoding='utf-8')
            record = json.loads(text)
            if record.get('status') != 'running':
                continue
            with path.with_name('live.log').open('rb') as stream:
                log = os.fstat(stream.fileno())
            if log != path.with_name('live.log').stat() or text != path.read_text(encoding='utf-8'):
                continue  # replaced/removed log or concurrent record save: no serving claim
            snapshots[path] = record, log
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return snapshots


def _leg_serving(runner, leg, previous=None):
    """Observe size growth across the probe; touching an old log is not growth."""
    now = time.time()
    for path, (record, log) in _run_logs(runner).items():
        try:
            current = record.get('chosen_leg') or record.get('leg')
            if not current:
                tried = {a['leg']['id'] if isinstance(a.get('leg'), dict) else a.get('leg')
                         for a in record.get('attempts', ())}
                current = next((l for l in record.get('plan', {}).get('legs', ()) if l['id'] not in tried), {})
            matches = (current.get('model') == leg.model and current.get('harness') == leg.harness
                       if isinstance(current, dict) else current == getattr(leg, 'id', None))
            last_growth = log.st_mtime  # first observation of an append log
            if previous is not None and path in previous:
                old_record, old = previous[path]
                if (log.st_dev, log.st_ino) != (old.st_dev, old.st_ino) or log.st_size < old.st_size:
                    continue
                last_growth = log.st_mtime if log.st_size > old.st_size else old.st_mtime
                if log.st_size == old.st_size and record != old_record:
                    continue
            if matches and log.st_size > 0 and 0 <= now - last_growth <= LIVE_RUN_WRITE_WINDOW_S:
                return True
        except (TypeError, KeyError, AttributeError):
            continue
    return False


def probe_env(base=None):
    """Git env for the probe's own git calls: config isolation, no redirection (D-3465)."""
    env = dict(os.environ if base is None else base, **GIT_ISOLATION)
    for name in GIT_REDIRECT_VARS:
        env.pop(name, None)
    return env


def caller_heads():
    """HEAD of the caller's cwd repo (if any) under both git resolutions (D-3465).

    ``'env'`` resolves the repo the caller's own git sees (its
    GIT_DIR/GIT_WORK_TREE honoured); ``'cwd'`` resolves the repo found from the
    process cwd alone. A leaked probe commit lands in one of the two; ``{}``
    when the caller's cwd is not in a repo (git errors are swallowed: this is
    a best-effort tripwire).
    """
    heads = {}
    for label, env in (('env', dict(os.environ)), ('cwd', probe_env())):
        try:
            heads[label] = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=os.getcwd(), env=env,
                stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return heads


def seconds(name, default, value=None, *, positive=False, parameter=None):
    label = parameter if value is not None and parameter else name
    try:
        value = float(os.environ.get(name, default) if value is None else value)
    except (TypeError, ValueError):
        raise ParameterRefused('invalid ' + label) from None
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ParameterRefused('invalid ' + label)
    return value


def key(leg):
    """One leg key for probe and quota bookkeeping (Q16/M8).
    Prefix only bare agy models; provider-qualified models are their own keys.
    Legacy rows without a harness keep their historical model-only keys.
    """
    return ('agy/' + leg.model if getattr(leg, 'harness', None) == 'agy' and '/' not in leg.model
            else leg.model)


def reset_iso(reset):
    """UTC ISO-8601 for a booked reset; 'unknown' when absent or unformattable.

    The 24 h reset cap is write-time only, so a legacy out-of-range reset_at
    must never raise from display or skip-reason code (D-3685).
    """
    if reset is None:
        return 'unknown'
    try:
        return datetime.fromtimestamp(reset, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return 'unknown'


def read_status(reader, model, **kw):
    """Read a bare model under both keys: an agy leg books 'agy/<model>', others the bare model (Q16)."""
    from plangraph.routing_support.liveness import unavailable
    state = reader(model, **kw)
    return state if '/' in model or unavailable(state) else reader('agy/' + model, **kw)


class DispatchReader:
    """Preserve adapter metadata while applying dispatch key lookup."""
    def __init__(self, reader, fallback):
        self.status = getattr(reader, 'leg_status', reader) or fallback
        self.enabled = getattr(reader, 'enabled', False)
        self.store = getattr(reader, 'store', None)

    def leg_status(self, model, **scope):
        return read_status(self.status, model, **scope)

    __call__ = leg_status


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


async def probe(runner, leg, *, timeout_s=None, store=None, role='implement'):
    """One real commit task in a disposable repo; infra failures book no leg state.

    D-3465: a moved caller HEAD reports ``ok=False``, "probe leaked into caller
    repo"; the caller's repo is never auto-reset.
    """
    timeout = seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, timeout_s, positive=True, parameter='timeout_s')
    state = store if store is not None else leg_state
    start = time.monotonic()
    # Missing executables describe this host, not the remote leg. Check each
    # harness independently, before creating a repo or attempting any leg.
    if shutil.which(leg.harness) is None:
        return dict(ok=False, state='host-not-ready',
                    reason='host-not-ready: harness missing: ' + leg.harness,
                    infra=True, seconds=time.monotonic() - start)
    deadline = start + timeout
    previous = await asyncio.to_thread(_run_logs, runner)
    root = tempfile.mkdtemp(prefix='plangraph-leg-probe-')
    job = None
    stderr = ''
    spawn_task = None
    owned_ids = set()

    def git(*args):
        return subprocess.check_output(['git', '-C', root, *args], env=probe_env(),
                                       stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip()

    try:
        caller_before = await asyncio.to_thread(caller_heads)
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
                from .probe_cleanup import OWNED
                OWNED.set(owned_ids)
                return await runner.spawn(prompt='Reply with exactly OK.' if role == 'review' else PROMPT, role=role, model_or_combo=leg.model,
                                         client=leg.harness, cwd=root, extra_env=CHILD_ISOLATION,
                                         **({'reasoning_cap': True} if role == 'review' and leg.family == 'mimo' else {}),
                                         watchdog=dict(stall_s=timeout + 60, first_edit_s=timeout + 60,
                                                       max_s=timeout + 60, interval=1))
            spawn_task = asyncio.ensure_future(launch())
            try:
                job = await asyncio.wait_for(asyncio.shield(spawn_task), max(deadline - time.monotonic(), 0))
            except asyncio.TimeoutError:
                ok, reason = False, 'timeout'
            except (ValueError, RuntimeError, OSError) as exc:
                ok, reason, infra = False, 'probe infra: spawn', True
                stderr = getattr(exc, 'stderr', '') or ''
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
                ok = (job.exit_code == 0 and job.stdout.strip() == 'OK') if role == 'review' else changed and 'ok.txt' in files
                reason = '' if ok else f'exit {job.exit_code}' if job.exit_code else 'no OK answer' if role == 'review' else 'no commit'
        # Tripwire (D-3465): the writer must never touch the caller's repo.
        caller_after = await asyncio.to_thread(caller_heads)
        leaked = any(caller_after.get(label) != head for label, head in caller_before.items())
        if leaked:
            # Report the leak; NEVER auto-reset the caller's repo here.
            ok, reason, infra = False, 'probe leaked into caller repo', False
        excerpt, serving = '', False
        if not ok and not infra:
            from .env_redact import redact_output, redact_env_values
            diagnostic = job or next((j for jid, j in getattr(runner, 'jobs', {}).items() if jid in owned_ids), None)
            stderr = getattr(diagnostic, 'stderr', '') or stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', 'replace')
            env = dict(os.environ)
            env.update(getattr(runner, '_child_envs', {}).get(getattr(diagnostic, 'job_id', None), {}))
            excerpt = ' '.join(leg_state._redact(redact_output(redact_env_values(stderr, env)), limit=None)[-400:].splitlines())
            serving = await asyncio.to_thread(_leg_serving, runner, leg, previous)
        if not infra:
            if ok:
                state.record_probe_pass(key(leg), caller='leg-probe')
            else:
                state.record_probe_fail(key(leg), reason, caller='review-leg-probe' if role == 'review' else 'leg-probe', serving=serving and role != 'review', stderr_excerpt=excerpt)
        reason += ': ' + excerpt if excerpt and not leaked else ''
        result = dict(ok=ok, reason=reason, infra=infra, seconds=time.monotonic() - start, stderr_excerpt=excerpt)
        if serving:
            result['state'] = 'probe_failed_leg_serving'
        return result
    finally:
        from .probe_cleanup import cleanup
        cleanup_task = asyncio.create_task(cleanup(runner, spawn_task, job, root, owned_ids))
        # A second caller cancellation must not interrupt tree cleanup.
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
        cleanup_task.result()

_INFLIGHT = {}

async def before_launch(runner, leg, role, dry_run):
    from .target import quota_paused
    from .env_redact import redact_output, redact_env_values
    root = (getattr(runner, 'state', None) or getattr(runner, '_state', None)
            or getattr(runner, 'state_root', None))
    if quota_paused(root, role, leg):
        return 'quota pause'
    if dry_run:
        return None
    ttl = seconds('PLANGRAPH_LEG_PROBE_TTL_S', 1800)
    if ttl and role in ('implement', 'fix'):
        seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, positive=True)
    state = _store_for(runner)
    adapter = state if getattr(state, 'enabled', False) else None
    state = adapter.store if adapter else state
    root = root or getattr(state, 'state_root', None)
    if not root and (path := getattr(state, 'path', None)):
        root = Path(path).parent
    if quota_paused(root, role, leg):
        return 'quota pause'
    def admission(retry=True, probe_ok=False):
        if adapter:
            status = adapter.leg_status(key(leg), account=getattr(leg, 'account', None), combo=getattr(leg, 'combo', None))
            reason = status.get('reason') or status['state']
            if reason.startswith('leg_state_snapshot_error:'):
                logging.getLogger(__name__).warning('%s: %s', key(leg), redact_output(redact_env_values(reason, os.environ)))
                if retry:
                    adapter._at = None
                    return admission(False, probe_ok)
                return None if probe_ok else reason
            if status['state'] != 'ok':
                return 'leg_state_snapshot_error' if reason.strip().lower() == 'unknown' else reason
        return None
    def limit():  # D-3685: booked provider limits win before and after probes.
        if (status := state.leg_status(key(leg))).get('state') == 'limited':
            return 'limited until ' + reset_iso((status.get('evidence') or {}).get('reset_at'))
    if booked := limit():
        return booked
    skip = admission()
    snapshot_error = (skip or '').startswith('leg_state_snapshot_error:')
    if not snapshot_error and (not ttl or role not in ('implement', 'fix')):
        return skip
    ts = None if snapshot_error else state.last_probe_ts(key(leg))
    if ts is not None and time.time() - ts < ttl:
        return skip  # Only ordinary admission may reuse pre-failure probe evidence.
    pending = _INFLIGHT.get(key(leg))  # Share concurrent probes, failures included.
    if pending is None:
        pending = _INFLIGHT[key(leg)] = asyncio.ensure_future(probe(runner, leg, store=state))
        pending.add_done_callback(lambda _: _INFLIGHT.pop(key(leg), None))
    result = await asyncio.shield(pending)
    reason = result.get('reason')
    if not reason or reason.strip().lower() == 'unknown':
        reason = 'host_not_ready' if result.get('state') == 'host-not-ready' else 'leg_probe_failed'
    if snapshot_error:
        return limit() or (None if result['ok'] else redact_output(redact_env_values(skip, os.environ)) + '; probe: ' + reason)
    if result.get('state') == 'host-not-ready':
        return reason
    if adapter:
        adapter._at = None  # Re-read after an ordinary probe.
    return (limit() or admission(probe_ok=result['ok'])) if result['ok'] or result.get('infra') or result.get('state') == 'probe_failed_leg_serving' else 'down: ' + reason

async def probe_many(runner, legs=(), all_writers=False, timeout=None, *, all_reviews=False):
    from plangraph.planner.legs import DEFAULT_LEGS, leg_active, spawnable_legs
    SPAWNABLE_LEGS = spawnable_legs()
    ids = {legs} if isinstance(legs, str) else set(legs or ())
    if not (ids or all_writers or all_reviews):
        raise ParameterRefused('select --leg ID or --all-writers')
    unknown = ids - {leg.id for leg in SPAWNABLE_LEGS}
    if unknown:
        raise ParameterRefused('unknown legs: ' + ', '.join(sorted(unknown)))
    timeout = seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, timeout, positive=True, parameter='--timeout')
    # --all-writers means the routed writers; an explicit leg is probed only by name.
    selected = [leg for leg in SPAWNABLE_LEGS if leg_active(leg) and
                (leg.id in ids or (all_writers and leg in DEFAULT_LEGS and
                                   any(role in leg.roles for role in ('implement', 'fix'))))]
    if all_reviews:
        from plangraph.routing_support.leg_state_flag import live_default_legs
        from plangraph.planner.route_policy import load_route_policy
        policy = load_route_policy()
        selected = [leg for leg in live_default_legs('review') if 'review' in leg.roles
                    and leg_active(leg) and not policy.skip_reason(leg, 'review')]
    results = []
    state = _store_for(runner)
    state = state.store if getattr(state, 'enabled', False) else state
    for leg in selected:
        # D-3685: a leg booked limited is known-unusable; probing it would only
        # spend a model run and let the probe failure mask the booked reset.
        status = state.leg_status(key(leg))
        if status.get('state') == 'limited':
            # Same row shape as a probed result (reason/seconds/infra included):
            # every probe-row reader keeps working on skipped rows.
            results.append(dict(id=leg.id, ok=False, reason='limited', infra=False, seconds=0.0,
                                skipped='limited',
                                reset_at=(status.get('evidence') or {}).get('reset_at')))
            continue
        results.append(dict(id=leg.id, **await probe(runner, leg, timeout_s=timeout, store=state,
            **({'role': 'review'} if all_reviews else {}))))
    return dict(ok=all(row['ok'] for row in results), probes=results)
