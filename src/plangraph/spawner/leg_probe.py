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

from plangraph.planner.legs import REVIEW_HYDROGEN
from plangraph.routing_support import leg_state
from plangraph.routing_support.leg_state import leg_key as key
from plangraph.routing_errors import ParameterRefused

from .host_ready import harness_path
from .env_redact import harness_limit_line, sandbox_denial
from .exit_outcome import quota_models

PROMPT = 'Create a file ok.txt containing the single word ok, then git add it and git commit -m probe. Do nothing else.'
REVIEW_PROMPT = 'Reply with the single word OK. Do not create a plan or any other file.'

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
    """Read legacy bare/agy keys, keeping the native Sonnet subscription separate."""
    from plangraph.routing_support.liveness import unavailable
    from plangraph.planner.legs import REVIEW_SONNET
    state = reader(model, **kw)
    return state if model == REVIEW_SONNET or '/' in model or unavailable(state) else reader('agy/' + model, **kw)


class DispatchReader:
    """Preserve adapter metadata while applying dispatch key lookup."""
    def __init__(self, reader, fallback):
        self.status = getattr(reader, 'leg_status', reader) or fallback
        self.enabled = getattr(reader, 'enabled', False)
        self.store = getattr(reader, 'store', None)
        self.last_probe_attempt_ts = getattr(reader, 'last_probe_attempt_ts', None)

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


def probe_quota_line(leg, review_mode, exit_code, stdout, stderr, refusal=None):
    """The limit line of a failed probe: a review probe's (agy, OpenCode), and any Codex probe's (its account limit)."""
    if leg.harness == 'opencode' and review_mode:
        # An OpenCode run is read the way a real run's exit is: without its progress lines. The display
        # output keeps only a partial answer that precedes the error frame, so the runner's confirmed
        # refusal (a refused dispatch, exit 7) is the evidence then.
        from .exit_outcome import without_progress
        return (harness_limit_line(exit_code, without_progress(stdout), without_progress(stderr))
                or (refusal if exit_code == 7 and isinstance(refusal, str) else None))
    if leg.harness == 'codex' or (review_mode and leg.harness == 'agy'):
        return harness_limit_line(exit_code, stdout, stderr)
    return None


def _is_review_probe(leg, role=None):
    if role is not None:
        return role == 'review'
    roles = getattr(leg, 'roles', ())
    return 'review' in roles and 'implement' not in roles and 'fix' not in roles


def _missing_harness(harness, seconds_elapsed=0.0):
    return dict(ok=False, state='host-not-ready',
                reason='host-not-ready: harness missing: ' + harness,
                infra=True, seconds=seconds_elapsed)


async def probe(runner, leg, *, timeout_s=None, store=None, role=None, quota_reprobe=False):
    """One real commit task (or tiny OK prompt for reviews) in a disposable dir; infra failures book no leg state.

    D-3465: a moved caller HEAD reports ``ok=False``, "probe leaked into caller
    repo"; the caller's repo is never auto-reset.
    """
    from .env_redact import redact_env_values, redact_output
    from .exit_outcome import _booking_seconds

    timeout = seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, timeout_s, positive=True, parameter='timeout_s')
    state = store if store is not None else leg_state
    review_mode = _is_review_probe(leg, role)
    start = time.monotonic()
    # Missing executables describe this host, not the remote leg. Check each
    # harness independently, before creating a repo or attempting any leg.
    if shutil.which(leg.harness, path=harness_path(os.environ)) is None:
        return _missing_harness(leg.harness, time.monotonic() - start)
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
        if not review_mode or leg.harness == 'codex':
            try:
                await asyncio.to_thread(git, 'init')
                if not review_mode:
                    hooks = Path(root) / 'probe-hooks'
                    hooks.mkdir()
                    await asyncio.to_thread(git, 'config', 'user.name', 'Plangraph probe')
                    await asyncio.to_thread(git, 'config', 'user.email', 'probe@plangraph.invalid')
                    # Repo-local settings also apply to the writer's own commit.
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
                # A Codex writer commits only where its sandbox may write the git dir (as pg-spawnd grants it).
                return await runner.spawn(
                    prompt=REVIEW_PROMPT if review_mode else PROMPT,
                    role='review' if review_mode else 'implement',
                    model_or_combo=leg.model,
                    client=leg.harness, cwd=root, extra_env=CHILD_ISOLATION,
                    writable_dirs=[str(Path(root) / '.git')] if leg.harness == 'codex' and not review_mode else (),
                    watchdog=dict(stall_s=timeout + 60, first_edit_s=timeout + 60,
                                  max_s=timeout + 60, interval=1))
            spawn_task = asyncio.ensure_future(launch())
            try:
                job = await asyncio.wait_for(asyncio.shield(spawn_task), max(deadline - time.monotonic(), 0))
            except asyncio.TimeoutError:
                ok, reason = False, 'timeout'
            except (ValueError, RuntimeError, OSError) as exc:
                cause = redact_output(redact_env_values(str(exc), os.environ))
                cause = ' '.join(leg_state._redact(cause, limit=400).splitlines())
                ok, reason, infra = False, f'probe infra: spawn: {type(exc).__name__}', True
                if cause:
                    reason += ': ' + cause
        committed = False  # the writer's ok.txt commit landed; set by the writer-probe check below
        if ok is None:
            try:
                await _await_completion(runner, job, deadline)
            except asyncio.TimeoutError:
                ok, reason = False, 'timeout'
        if ok is None:
            if review_mode:
                exhausted = any(e.get('event') == 'opencode_reasoning_exhausted'
                                for e in (getattr(job, 'events', None) or ()))
                answer = (getattr(job, 'stdout', None) or '').strip()
                answered = answer == 'OK' if quota_reprobe or getattr(leg, 'id', None) == REVIEW_HYDROGEN else bool(answer)
                ok = (getattr(job, 'status', None) == 'completed'
                      and not getattr(job, 'exit_code', 0)
                      and answered and not exhausted)
                reason = ('' if ok else
                          'reasoning_output_limit' if exhausted else
                          f'exit {job.exit_code}' if getattr(job, 'exit_code', None) else
                          'empty output' if not answer else
                          'unexpected review answer' if not answered else
                          str(getattr(job, 'status', 'failed')))
            else:
                try:
                    changed = before != await asyncio.to_thread(git, 'rev-parse', 'HEAD')
                    files = (await asyncio.to_thread(git, 'ls-tree', '--name-only', 'HEAD')).splitlines()
                except (OSError, subprocess.CalledProcessError):
                    ok, reason, infra = False, 'probe infra: git', True
                else:
                    # The commit alone is not a pass: the writer process must
                    # also have completed with exit code zero.
                    status, code = getattr(job, 'status', None), getattr(job, 'exit_code', None)
                    committed = changed and 'ok.txt' in files
                    ok = committed and status == 'completed' and code == 0
                    reason = ('' if ok else f'exit {code}' if code else
                              'no commit' if not (changed and 'ok.txt' in files) else
                              'missing exit code' if status == 'completed' and code is None else
                              str(status or 'failed'))
        # Tripwire (D-3465): the writer must never touch the caller's repo.
        caller_after = await asyncio.to_thread(caller_heads)
        leaked = any(caller_after.get(label) != head for label, head in caller_before.items())
        if leaked:
            # Report the leak; NEVER auto-reset the caller's repo here.
            ok, reason, infra = False, 'probe leaked into caller repo', False
        excerpt, serving, quota = '', False, None
        if not ok and not infra:
            diagnostic = job or next((j for jid, j in getattr(runner, 'jobs', {}).items() if jid in owned_ids), None)
            stderr = getattr(diagnostic, 'stderr', '') or stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', 'replace')
            env = dict(os.environ)
            env.update(getattr(runner, '_child_envs', {}).get(getattr(diagnostic, 'job_id', None), {}))
            excerpt = ' '.join(leg_state._redact(redact_output(redact_env_values(stderr, env)), limit=None)[-400:].splitlines())
            if not leaked and diagnostic is not None:
                quota = probe_quota_line(leg, review_mode, getattr(diagnostic, 'exit_code', None),
                                         getattr(diagnostic, 'stdout', '') or '', stderr,
                                         getattr(diagnostic, 'refusal', None))
                if quota:
                    quota = redact_output(redact_env_values(quota, env))
            if not quota and not leaked and leg.harness == 'codex' and not review_mode and not committed and diagnostic is not None \
                    and (sandbox_denial(stderr, getattr(diagnostic, 'stdout', ''))
                         or any(e.get('event') == 'sandbox_denied' for e in getattr(diagnostic, 'events', None) or ())):
                # The probe's sandbox blocked the writer (also when it then ran out of time): never a leg failure.
                # A landed commit means the sandbox let the write through, so a nonzero exit stays a leg failure.
                ok, reason, infra, excerpt = False, 'probe infra: sandbox denied a write', True, ''
            elif not quota:
                serving = await asyncio.to_thread(_leg_serving, runner, leg, previous)
        if not infra:
            caller = 'review-probe' if review_mode else 'leg-probe'
            if ok:
                state.record_probe_pass(key(leg), caller=caller)
            elif quota:
                for model in quota_models(key(leg), leg.harness):
                    state.record_harness_limit(model, quota, caller, harness=leg.harness,
                                               down_s=_booking_seconds())
                status = state.leg_status(key(leg))
                if status['state'] == 'limited':
                    return dict(ok=False, reason='limited', skipped='limited', infra=False,
                                seconds=time.monotonic() - start, stderr_excerpt=excerpt,
                                reset_at=status['evidence']['reset_at'])
            else:
                state.record_probe_fail(key(leg), reason, caller=caller, serving=serving, stderr_excerpt=excerpt)
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

class ProbeBudget:
    """Time the live probes of one spawn may use together, below the base admission window.

    The pre-launch probe runs inside the spawn's base admission wait (ADMISSION_TIMEOUT). One
    probe gets at most a third of the window and all probes of a spawn at most half, so serial
    hung probes fail over leg by leg and still leave time to prepare the worktree and launch.
    A probe that exceeds its share is killed and booked as a probe failure.
    """
    def __init__(self, window=None, started=None):
        from .base_fresh import ADMISSION_TIMEOUT
        from .base_fresh import ADMISSION_STARTED
        window = ADMISSION_TIMEOUT if window is None else window
        # Admission time already spent: since the wait began (monotonic; routing and record
        # creation included) and since the run record was created, whichever is longer.
        waited = time.monotonic() - ADMISSION_STARTED.get() if ADMISSION_STARTED.get() else 0
        recorded = time.time() - started if started else 0
        elapsed = max(waited, recorded, 0)
        self.each = window / 3
        # Half the window in all, and always leaving a sixth for the worktree and the launch.
        self.left = min(window / 2, window - window / 6 - elapsed)
        self.grace = min(5, self.each / 6)  # probe setup and cleanup around its own deadline

    def timeout(self):
        """Seconds the next probe may run, or 0 when the spawn's probe budget is spent."""
        return max(min(seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, positive=True), self.each, self.left), 0)

    def spend(self, started):
        self.left -= time.monotonic() - started


def route_probe_scope(plan, leg):
    for candidate in getattr(plan, 'candidates', ()):
        if candidate['leg'] == leg.id:
            reason = candidate.get('leg_state_reason') or ''
            if reason.startswith('leg_state_snapshot_error:'):
                return {'route_state_reason': reason}
            break
    return {}


async def before_launch(runner, leg, role, dry_run, budget=None, *, route_state_reason=None):
    from plangraph.routing_support.liveness import read_leg_model
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
    scoped_model = key(leg)
    retry_route_snapshot = scoped_model != leg.model and (route_state_reason or '').startswith('leg_state_snapshot_error:')
    def admission(retry=True, probe_ok=False):
        if adapter:
            scope = dict(account=getattr(leg, 'account', None), combo=getattr(leg, 'combo', None))
            status = read_leg_model(adapter.leg_status, leg, scoped_model, **scope)
            if status['state'] == 'ok' and retry_route_snapshot:
                status = read_leg_model(adapter.leg_status, leg, leg.model, **scope)
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
    if (skip or '').startswith('auth_refused:') or skip == 'not_listed':
        return skip  # An unlisted model has nothing to run; a probe cannot admit it.
    snapshot_error = (skip or '').startswith('leg_state_snapshot_error:')
    if not snapshot_error and (not ttl or role not in ('implement', 'fix')):
        return skip
    ts = None if snapshot_error else state.last_probe_ts(key(leg))
    if ts is not None and time.time() - ts < ttl:
        return skip  # Only ordinary admission may reuse pre-failure probe evidence.
    budget = budget or ProbeBudget()
    timeout = budget.timeout()
    if timeout <= 0:
        return 'probe_budget_exhausted'
    started = time.monotonic()
    pending = _INFLIGHT.get(key(leg))  # Share concurrent probes, failures included.
    joined = pending is not None
    if pending is None:
        pending = _INFLIGHT[key(leg)] = asyncio.ensure_future(probe(runner, leg, store=state, timeout_s=timeout))
        pending.add_done_callback(lambda _: _INFLIGHT.pop(key(leg), None))
    try:
        # The probe bounds itself to `timeout`; every spawn, the one that started the probe and
        # any that join it, waits only as long as its own budget allows (the owner a short grace
        # more for the probe's setup and cleanup) and is charged for the time it waited. A probe
        # that outlives the wait keeps running for its other waiters.
        result = await asyncio.wait_for(asyncio.shield(pending), timeout if joined else timeout + budget.grace)
    except asyncio.TimeoutError:
        return 'probe_budget_exhausted'
    finally:
        budget.spend(started)
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

def _check_manage_key(runner):
    binary = shutil.which('pg-spawnd')
    if binary is None:
        return None
    root = (getattr(runner, 'state', None) or getattr(runner, '_state', None)
            or getattr(runner, 'state_root', None))
    cmd = [binary, 'check-manage-key', '--json']
    if root is not None:
        cmd.extend(['--state', str(root)])
    try:
        proc = subprocess.run(cmd, check=False, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            if data.get('emitted'):
                logging.getLogger(__name__).warning('missing_manage_key')
            data.pop('key_path', None)
            return data
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logging.getLogger(__name__).warning('manage_key_check_failed: %s', type(exc).__name__)
    return None


async def probe_many(runner, legs=(), all_writers=False, timeout=None, *, all_reviewers=False, role=None):
    """Probe selected legs and report quota skips separately from probe failures."""
    from plangraph.planner.legs import DEFAULT_LEGS, leg_active, routed_review_legs, spawnable_legs
    SPAWNABLE_LEGS = spawnable_legs()
    ids = {legs} if isinstance(legs, str) else set(legs or ())
    if not (ids or all_writers or all_reviewers):
        raise ParameterRefused('select --leg ID, --all-writers or --all-reviewers')
    unknown = ids - {leg.id for leg in SPAWNABLE_LEGS}
    if unknown:
        raise ParameterRefused('unknown legs: ' + ', '.join(sorted(unknown)))
    timeout = seconds('PLANGRAPH_LEG_PROBE_TIMEOUT_S', 180, timeout, positive=True, parameter='--timeout')
    # --all-writers means the routed writers; --all-reviewers means the routed reviewers.
    selected = []
    seen = set()
    review_ids = set()
    def add(leg, review=False):
        if leg.id not in seen:
            seen.add(leg.id)
            selected.append(leg)
            if review:
                review_ids.add(leg.id)
    for leg in SPAWNABLE_LEGS:
        if leg_active(leg) and (leg.id in ids or (all_writers and leg in DEFAULT_LEGS and
                                                  any(r in leg.roles for r in ('implement', 'fix')))):
            # A named Hydrogen probe checks the D-6550 review seat. Writer sweeps still probe commits.
            add(leg, review=leg.id == REVIEW_HYDROGEN and leg.id in ids and not all_writers and role is None)
    if all_reviewers:
        for leg in routed_review_legs():
            add(leg, review=True)
    path = harness_path(os.environ)
    if selected and not any(shutil.which(harness, path=path) for harness in {leg.harness for leg in selected}):
        return dict(ok=False, probes=[dict(id=leg.id, **_missing_harness(leg.harness)) for leg in selected],
                    limited_legs=[])
    manage_key = await asyncio.to_thread(_check_manage_key, runner) if (all_reviewers or role == 'review') else None
    results = []
    state = _store_for(runner)
    state = state.store if getattr(state, 'enabled', False) else state
    scheduled = all_reviewers and not all_writers and not ids
    for leg in selected:
        model = key(leg)
        status = (state.agy_probe_status(model)
                  if scheduled and leg.harness == 'agy' else state.leg_status(model))
        evidence = status.get('evidence') or {}
        limited = status.get('state') == 'limited'
        quota_reprobe = False
        if (limited and scheduled and leg.harness == 'agy' and evidence.get('status') == 429
                and leg_state.routing_status(status).get('state') == 'limited'):
            now = time.time()
            last_attempt = state.last_probe_attempt_ts(model, now=now)
            if evidence.get('caller') == 'review-probe':
                last_attempt = max(last_attempt or 0, evidence['ts'])
            quota_reprobe = last_attempt is None or now - last_attempt >= 3600
        if limited and not quota_reprobe:
            # Same row shape as a probed result (reason/seconds/infra included):
            # every probe-row reader keeps working on skipped rows.
            results.append(dict(id=leg.id, ok=False, reason='limited', infra=False, seconds=0.0,
                                skipped='limited',
                                reset_at=evidence.get('reset_at')))
            continue
        # Honor the recorded review selection for dual-role legs.
        probe_role = 'review' if quota_reprobe or role == 'review' or leg.id in review_ids else role
        probe_kwargs = {'role': probe_role} if probe_role is not None else {}
        if quota_reprobe:
            probe_kwargs['quota_reprobe'] = True
        results.append(dict(id=leg.id, **await probe(runner, leg, timeout_s=timeout, store=state, **probe_kwargs)))
    reply = dict(ok=all(row['ok'] for row in results if not row.get('skipped')),
                 probes=results,
                 limited_legs=[row['id'] for row in results if row.get('skipped') == 'limited'])
    if manage_key is not None:
        reply['manage_key_alert'] = manage_key
    return reply
