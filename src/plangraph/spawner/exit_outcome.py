"""Shared dispatch outcome policy for base and routed launches."""
import atexit
import json
import os
import shutil
import socket
import subprocess
import time
from functools import lru_cache

from plangraph.routing_support.leg_state import _down_seconds
from .env_redact import early_refusal, first_reason, harness_limit_line, unavailable_reason

#: Verdicts a guard or a cancel owns.  No later fallthrough outcome may stomp
#: them and terminal_reason never pops them (M9/M15, Q14).
TERMINAL_REASONS = ('git_config_mutated', 'writer_no_changes', 'cancelled', 'reasoning_output_limit',
                    'tool_loop', 'stuck', 'disk_run_stopped')


def without_progress(text):
    """OpenCode tool diagnostics are not failure evidence (including ANSI output)."""
    from .env_redact import normalize
    return '\n'.join(line for line in text.splitlines()
                     if not normalize(line).lstrip().startswith(('✱', '→', '>', '$')))


def is_opencode(job):
    from pathlib import Path
    command = getattr(job, 'command', ()) if job is not None else ()
    return bool(command and Path(command[0]).name == 'opencode')


def failure_reason(job, stop=None, *, harness=None):
    for event in getattr(job, 'events', ()):
        if event.get('event') == 'inline_review_no_verdict':
            return 'no_verdict: ' + event['reason']
    opencode = harness == 'opencode' or is_opencode(job)
    text = without_progress(job.stderr) if opencode else job.stderr
    reason = first_reason(text)
    if not reason and opencode:
        reason = next((first_reason(e.get('message') or '') for e in reversed(getattr(job, 'events', ()))
                       if e.get('event') == 'opencode_error' and e.get('message')), None)
    if not reason and opencode:
        for line in getattr(job, 'stdout', '').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            error = event.get('error') if isinstance(event, dict) and event.get('type') == 'error' else None
            if isinstance(error, dict) and isinstance(error.get('message'), str):
                reason = first_reason(error['message']) or reason
    return reason or ('provider_stop' if stop else f'harness exit code {job.exit_code}')


def origin_refusal(record):
    """A native refusal forwarded to the origin host: the target host sent its notification, the origin sends none."""
    return bool(record.get('origin_receipt') and record.get('run_kind') == 'native-codex' and record.get('status') == 'refused')


def refusal_record(record, job):
    """Persist a safety stop at dispatch, not in the fallthrough classifier."""
    disk = next((e for e in getattr(job, 'events', ()) if e.get('event') == 'disk_run_stopped'), None)
    if disk is not None and job.status not in ('cancelled', 'stalled', 'timeout'):
        record.update(status='failed', exit_code=job.exit_code or 7,
                      reason='kill_incomplete' if getattr(job, 'survivors', ()) else 'disk_run_stopped',
                      disk_refusal=disk['reason'], events=record.get('events', []) + job.events)
        if getattr(job, 'survivors', ()):
            record['survivors'] = job.survivors
        return True
    refusal = next((e for e in getattr(job, 'events', ()) if e.get('event') == 'model_refusal'), None)
    if refusal is None and job.status != 'model_refusal' and record.get('reason') != 'model_refusal':
        return False
    reason = record.get('reason')
    if reason in (None, 'harness_limit', 'credit_limit', 'usage_limit', 'tool_call_only'):
        reason = 'model_refusal'
    record.update(status='failed', exit_code=job.exit_code or 1, reason=reason,
                  events=record.get('events', []) + getattr(job, 'events', []))
    if refusal is not None:
        record.update({k: refusal[k] for k in ('stop_reason', 'refusal_text_chars') if k in refusal})
    return True


def _booking_seconds():
    """H1: the booked-down window is clamped to [60, 86400] seconds.

    PLANGRAPH_LEG_DOWN_S accepts extremes like '0'/'1e-9' (the leg would never
    come back down) and '1e308' (near-permanent); a booking is at least one
    minute and at most one day.  Invalid values still fall back to 1800 inside
    _down_seconds (leg_state.py stays untouched).
    """
    return max(60.0, min(_down_seconds(None), 86400.0))


def _run_hook(command, event):
    """The hook thread's body: deliver, then leave the pending list, so a long-lived process keeps no finished threads."""
    import threading
    try:
        _deliver_cmd(command, event)
    finally:
        try:
            _PENDING_HOOKS.remove(threading.current_thread())
        except ValueError:
            pass


def _deliver_cmd(command, event):
    """H2: command-hook delivery on a daemon thread; a failure never fails the run."""
    import logging
    from . import notify
    try:
        notify.CommandSink(command).deliver('parent', event)
    except Exception:
        logging.getLogger(__name__).exception('leg refusal command notify failed')


#: F3 + H2: LEG_REFUSED hook threads that may still run; the caller never waits for them, the process does at exit.
_PENDING_HOOKS = []


@atexit.register
def _join_pending_hooks(budget=2.0):
    """At interpreter exit, wait up to ``budget`` seconds in total for hook threads still running, so a process
    that exits right after a refusal keeps its hook line, while a hanging hook delays the exit by at most 2 s."""
    import time
    end = time.monotonic() + budget
    for worker in list(_PENDING_HOOKS):
        worker.join(max(0.0, end - time.monotonic()))


def notify_leg_refused(service, record, chosen, reason):
    """D-3605 (a): tell the parent at once that a leg refused; delivery never fails the run.

    Lives here so the base and routed leg loops share exactly one LEG_REFUSED
    line per refused attempt (G3); a service without a completion sink stays
    silent.  The completion sink is a local file write and stays synchronous;
    only the PLANGRAPH_SPAWN_NOTIFY_CMD hook (a subprocess with a 10 s timeout)
    runs on a daemon thread so it never blocks the event loop (H2).  F3: the
    process joins that thread at exit (atexit, 2 s in total), so a process
    exiting right after the refusal still delivers the hook line; the caller
    never waits, and a hanging command is abandoned after 2 s at exit.  Only a
    forced termination (os._exit, SIGKILL) skips that join and can lose it.
    """
    import logging
    import os
    import threading
    from . import notify
    from .leg_probe import key as leg_key
    sink = getattr(service, 'completion_sink', None)
    if sink is None:
        return
    try:
        event = notify.leg_refused_event(record, leg_key(chosen), reason)
        sink.deliver('parent', event)
        command = os.environ.get('PLANGRAPH_SPAWN_NOTIFY_CMD')
        if command:
            worker = threading.Thread(target=_run_hook, args=(command, event), daemon=True)
            _PENDING_HOOKS.append(worker)   # before start: the worker removes itself when done; F3 joins at exit (H2)
            worker.start()
    except Exception:
        logging.getLogger(__name__).exception('leg refusal notify failed')


def _limit_target(service):
    from . import leg_state
    target = service.leg_reader
    if not hasattr(target, 'record_harness_limit'):
        target = getattr(target, '__self__', None)
    return target if hasattr(target, 'record_harness_limit') else leg_state


def quota_models(model, harness):
    """The models one usage limit books: a Codex limit is account-wide, so every routed Codex leg
    (its model and its id, the keys the route reads, review seats and writers alike) is limited with the run's own model."""
    if harness != 'codex':
        return [model]
    from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS, review_route_pool
    keys = [model]
    for leg in (*review_route_pool(), *DEFAULT_LEGS, *EXPLICIT_LEGS):
        if leg.harness == 'codex':
            keys += [leg.model, leg.id]
    return list(dict.fromkeys(keys))


def _book_failure(service, chosen, line, card=None):
    """Book legacy evidence and quarantine the enabled reader's cached identity.

    The adapter replaces its served set on authoritative refresh, so this
    observation is transient, not a second availability catalog. Read the
    injected adapter flag only; disabled readers retain legacy store semantics.
    """
    from plangraph.routing_support.leg_state import LegAdapter
    from .leg_probe import key
    target = _limit_target(service)
    scope = {name: card[name] for name in ('account', 'combo') if name in (card or {})}
    harness = getattr(chosen, 'harness', None)
    for model in quota_models(key(chosen), harness):
        target.record_harness_limit(model, line, 'spawner:harness', down_s=_booking_seconds(),
                                    harness=harness, **scope)
    if isinstance(target, LegAdapter) and target.enabled:
        target.quarantine(key(chosen))


def tool_call_only(job, record):
    """Recognize a whole text-call response, never a mention in real prose."""
    import re
    import subprocess
    from .env_redact import normalize
    events = getattr(job, 'events', ())
    protected = ('git_config_mutated', 'cancelled')
    if (record.get('reason') in protected or record.get('reason') == 'kill_incomplete'
            or any(e.get('event') in protected for e in [*events, *record.get('events', ())])
            or getattr(job, 'status', None) not in ('completed', 'failed')):
        return False
    text = without_progress(normalize(job.stdout))
    if not re.fullmatch(r'\s*(?:<tool_call>\s*<function=[^<>]+>(?:(?!</?tool_call>|</function>).)*'
                        r'</function>\s*</tool_call>\s*)+', text, re.S):
        return False
    if getattr(job, 'role', None) == 'implement':
        baseline = next((e for e in events if e.get('event') == 'writer_baseline'), {})
        if 'commits' in baseline and getattr(job, 'writer_email', None):
            from .opencode_isolation import writer_commits
            try:
                if writer_commits(job.cwd, job.writer_email) - set(baseline['commits']):
                    return False
            except (OSError, subprocess.SubprocessError):
                return False  # do not override uncertain commit evidence
    return True


def review_no_verdict(text, finish=None, *, toolless=False, require_files=True):
    """Explain why a structurally successful review answer cannot count."""
    from plangraph.review.verdict import has_files_read, has_tool_call_markup, judge
    if toolless and has_tool_call_markup(text):
        return 'no_verdict: toolless_tool_markup'
    verdict = judge(text, finish).verdict
    if verdict is None:
        return 'no_verdict'
    if require_files and verdict == 'pass' and (toolless or not has_files_read(text)):
        return 'no_verdict: files_read_missing'
    return None


def fallthrough_reason(service, chosen, job, record, *, card=None):
    if any(e.get('event') == 'disk_run_stopped' for e in getattr(job, 'events', ())):
        refusal_record(record, job)
        return None  # host capacity never books a provider down or launches another leg
    from .question_bridge import question_exit
    if question_exit(job, record):
        return None
    if record.get('reason') == 'model_refusal' or getattr(job, 'status', None) == 'model_refusal' or any(e.get('event') == 'model_refusal' for e in getattr(job, 'events', ())):
        return None   # safety stops are not provider limits or leg-health evidence
    # Before the terminal check: text-call output replaces writer_no_changes; tool_call_only keeps the other terminals.
    if tool_call_only(job, record):
        job.events = [e for e in getattr(job, 'events', ()) if e.get('event') != 'writer_no_changes']
        record['events'] = [e for e in record.get('events', ()) if e.get('event') != 'writer_no_changes']
        record['reason'] = 'tool_call_only'
        return 'tool_call_only'
    if record.get('reason') in (*TERMINAL_REASONS, 'kill_incomplete') or any(
            e.get('event') in TERMINAL_REASONS
            for e in (*record.get('events', ()), *getattr(job, 'events', ()))):
        return None   # terminal verdicts must stop dispatch, not merely keep their name
    streams = (job.stdout, job.stderr)
    if record.get('role') == 'review':
        # A diagnostic label alone is not quota evidence; validate its payload.
        streams = tuple('\n'.join(line.split(':', 1)[1].strip()
                        if line.strip().startswith(('usage_limit:', 'credit_limit:')) else line
                        for line in text.splitlines()) for text in streams)
    if is_opencode(job):
        streams = tuple(map(without_progress, streams))
    line = harness_limit_line(job.exit_code, *streams)
    if not line and record.get('role') == 'review':
        # A provider refusal is authoritative even when its CLI exits zero.
        line = early_refusal('\n'.join(streams)) or harness_limit_line(1, *streams)
    if not line and getattr(job, 'refusal', None) and job.exit_code == 7:
        # F3: a confirmed refusal (e.g. a 402 provider envelope whose reason is
        # not in the captured output) books and notifies like any harness limit.
        line = job.refusal
    if line:
        # Q16/M8: one leg key shared with the probe path -- 'agy/<model>' only
        # for an agy leg's bare model.
        # D-3605 (a): every harness-limit line without a provider reset time
        # is booked down for the validated PLANGRAPH_LEG_DOWN_S window (1800
        # on invalid/negative/non-finite), clamped to [60, 86400] s (H1);
        # 'Resets in ...' lines keep their own reset because
        # record_harness_limit applies down_s only when the line carries no
        # reset time.
        _book_failure(service, chosen, line, card)
        # M9/M15: a guard or cancel verdict is never stomped by a fallthrough.
        if record.get('reason') not in TERMINAL_REASONS:
            record['reason'] = 'harness_limit'
        reason = 'harness_limit: ' + line
        for status, token in ((429, 'usage_limit'), (402, 'credit_limit')):
            if line.startswith(f'HTTP {status}: {token}'):
                record['reason'] = token
                reason = token + ': ' + line
                break
        notify_leg_refused(service, record, chosen, reason)
        return reason
    streams = (job.stdout, job.stderr)
    reason = unavailable_reason(job.exit_code, *(map(without_progress, streams) if is_opencode(job) else streams))
    if reason is not None:
        # not_served_on_host is a fact about this host's environment (a detached unit without the
        # login opencode config), not provider evidence: never book it as a 429 down window.
        if 'not_served_on_host' not in reason.lower():
            _book_failure(service, chosen, 'model_unavailable: ' + reason, card)
        notify_leg_refused(service, record, chosen, reason)
    stop = getattr(getattr(service, 'runner', None), 'provider_responses', {}).get(getattr(job, 'job_id', None))
    if (not reason and record.get('role') == 'review' and stop
            and not getattr(job, 'survivors', ())
            and job.status in ('failed', 'skipped', 'limited')):
        return 'provider_stop'
    if (not reason and record.get('role') == 'review' and job.status == 'completed'
            and job.exit_code == 0 and not stop):
        from .prompt_input import TOOLLESS_MODES
        toolless = (getattr(chosen, 'tool_mode', None) in TOOLLESS_MODES
                    or getattr(job, '_inline_review', False))
        return review_no_verdict(job.stdout, getattr(job, 'response_finish_reason', None),
                                 toolless=toolless,
                                 require_files=record.get('review_document') != 'train_delta')
    # Review/seat failures exhaust the existing route, not the whole run.
    # Ordinary failures carry diagnostics, not booking/notification evidence.
    return reason or (failure_reason(job, harness=getattr(chosen, 'harness', None)) if record.get('role') == 'review'
                      and not getattr(job, 'survivors', ())
                      and job.status in ('failed', 'skipped', 'limited') else None)


def release_attempts(service, record):
    """Q13/M8: a run releases every attempt's job when it ends.

    The per-run registry is keyed by job id; popping only the record's last
    ``job_id`` leaked earlier fallthrough attempts -- every sample kept
    re-saving a stale record and a finished monitor task stayed registered,
    so a later cancel hit the wrong task.  Every job registered to this
    record is released from the registry and the runner's monitor table.
    """
    running = getattr(service, '_running_records', None) or {}
    tasks = getattr(getattr(service, 'runner', None), '_tasks', None) or {}
    for job_id in [jid for jid, owner in list(running.items()) if owner is record]:
        running.pop(job_id, None)
        tasks.pop(job_id, None)


def terminal_reason(record, job=None):
    """Q8/M15: the run's reason describes how the run ENDED.

    A 'harness_limit' stamped by an earlier fallthrough leg never survives a
    later attempt's outcome.  Guard evidence names the run (M9), and guard and
    cancel verdicts are terminal: never stomped, never popped (M15, Q14).
    """
    if record.get('status') == 'needs_input':
        return
    if record.get('reason') == 'kill_incomplete':
        return   # survivors outrank every other verdict: the run failed
    events = record.get('events') or ()
    special = next((e['event'] for e in events
                    if e.get('event') in ('disk_run_stopped', 'git_config_mutated', 'writer_no_changes')), None)
    if special is not None:
        record['reason'] = special
        return
    real = [a for a in record.get('attempts', ()) if a.get('status') != 'skipped']
    leg = real[-1].get('leg') if real else None
    leg = leg if isinstance(leg, dict) else {}
    opencode = is_opencode(job) or (record.get('chosen_leg') or leg).get('harness') == 'opencode'
    last_reason = real[-1].get('skip_reason') if real else None
    if opencode and last_reason:
        last_reason = without_progress(last_reason)
    if opencode:
        for field in ('reason', 'error'):
            if record.get(field):
                record[field] = without_progress(record[field])
    reason = record.get('reason')
    if reason in TERMINAL_REASONS:
        return
    if record.get('status') in ('failed', 'refused'):
        diagnostic = '\n'.join(record.get(key) or '' for key in ('error', 'stderr', 'stderr_tail'))
        diagnostic += '\n' + (getattr(job, 'stderr', '') or '')
        for named in ('prompt_too_large', 'stdin_prompt_flag_missing'):
            if named + ':' in diagnostic:
                record['reason'] = named
                return
    if record.get('role') == 'review' and record.get('status') == 'failed' and leg.get('harness') == 'agy':
        from .opencode_answer import AGY_MISSING_RESULT
        stderr = real[-1].get('stderr_tail') or ''
        if AGY_MISSING_RESULT in stderr.splitlines():
            record.update(reason='no_verdict', review_status='no_verdict', review_reason=AGY_MISSING_RESULT)
            return
    if (record.get('role') == 'review' and record.get('status') == 'failed'
            and (last_reason or '').startswith('no_verdict')):
        record.update(reason=last_reason, review_status='no_verdict')
        return
    if (record.get('role') == 'review' and record.get('status') == 'completed'
            and record.get('exit_code') in (None, 0) and (job is not None or real)):
        from .prompt_input import TOOLLESS_MODES
        # A persisted attempt still supplies its verdict text. Require file
        # evidence at dispatch so replay can preserve older valid receipts.
        text = job.stdout if job is not None else real[-1].get('stdout_tail', '')
        chosen = record.get('chosen_leg') or leg
        toolless = job is not None and (chosen.get('tool_mode') in TOOLLESS_MODES
                                       or getattr(job, '_inline_review', False))
        if missing := review_no_verdict(text, getattr(job, 'response_finish_reason', None),
                                       toolless=toolless,
                                       require_files=job is not None and record.get('review_document') != 'train_delta'):
            record.update(status='failed', exit_code=6, reason=missing, review_status='no_verdict')
            return
    ended_ok = record.get('exit_code') in (None, 0) and record.get('status') in ('completed', 'dry_run')
    if reason in ('harness_limit', 'tool_call_only'):
        if ended_ok:
            record.pop('reason', None)   # the fallthrough stamp is stale
            return
        # Q8: the final attempt's outcome wins over an earlier fallthrough; a
        # run that ended ON the quota leg keeps 'harness_limit'.
        real = [a for a in (record.get('attempts') or ()) if a.get('status') != 'skipped']
        last = real[-1] if real else None
        if last is not None:
            # r3: 'timed_out' is the review wall-cap spelling of 'timeout'
            if last.get('status') in ('timeout', 'timed_out'):
                reason = 'timeout'
            elif not (last_reason or '').startswith('harness_limit'):
                reason = last_reason or reason
        record['reason'] = reason
        return
    if record.get('exit_code') not in (None, 0):
        stderr = record.get('stderr_tail', '')
        if opencode:
            stderr = without_progress(stderr)
        stdout = getattr(job, 'stdout', '') if job is not None else (real[-1].get('stdout_tail', '') if real else record.get('stdout_tail', ''))
        if (record.get('exit_code') == 1 and record.get('status') == 'failed'
                and stdout.strip() and not stderr.strip() and not reason
                and not stdout.lstrip().startswith(('{', '['))):
            record['reason'] = 'exit1_after_stdout'
            return
        record['reason'] = (reason or last_reason or record.get('error')
                            or first_reason(stderr)
                            or f"harness exit {record['exit_code']}")


def orphaned(record, now):
    """Q21/M14: a 'running' record whose supervisor process is gone and that recorded no exit ends
    'failed', reason 'orphaned'; one that recorded its child's exit keeps that real exit and output."""
    import os
    path = record.get('supervisor_stderr_path')
    # os.kill(pid, 0) is TerminateProcess on Windows: never probe there.
    if record.get('status') != 'running' or record.get('ended') is not None or not path or os.name == 'nt':
        return None
    pid = None
    try:
        with open(str(path) + '.pid') as stream:
            pid = int(stream.read())
        from .proc_kill import _stat
        current = _stat(pid) if record.get('supervisor_pid_start') is not None else None
        if current is not None and current[4] != record['supervisor_pid_start']:
            raise ProcessLookupError   # pid belongs to a replacement, not this supervisor
        os.kill(pid, 0)
        return None
    except PermissionError:
        return None   # alive, owned by another user
    except (OSError, ValueError):
        pass
    if not os.path.exists(str(path) + '.pid'):
        return None   # no pid evidence: not proof the supervisor died
    if record.get('exit_code') is not None:
        # The child's exit was recorded before the supervisor went away: keep its real outcome.
        return dict(record, status='completed' if record['exit_code'] == 0 else 'failed', ended=now)
    return dict(record, status='failed', exit_code=1, ended=now, reason='orphaned',
                error=f'orphaned: supervisor pid {pid} is gone')


def queue_stale_reason(record, *, ownerless=True):
    """Why a 'queued' record has no live owner, else None.

    A queued run is owned by the process that waits for room (``owner_pid``) or, for a supervised
    queue, by its supervisor. A legacy record with neither is ownerless: nothing re-checks it."""
    import os
    if record.get('status') != 'queued' or record.get('ended') is not None or record.get('overflow_remote'):
        return None
    pid = record.get('owner_pid')
    if pid is None:
        if record.get('supervisor_stderr_path'):
            return None   # a supervised queue is judged by _expire_queue
        return 'queued record has no owner process; nothing re-checks its admission' if ownerless else None
    if os.name == 'nt' or type(pid) is not int:
        return None
    from .proc_kill import alive
    if alive(pid, record.get('owner_pid_start')):
        return None
    return f'queue owner pid {pid} is gone; the run will never be re-admitted'


def run_pids_dead(record):
    """True when every recorded process (child, supervisor) of a run is gone; no recorded pid is no proof."""
    import os
    if os.name == 'nt':
        return False
    from .proc_kill import alive
    proof = False
    pid = record.get('pid')
    if type(pid) is int:
        if alive(pid, record.get('pid_start')):
            return False
        proof = True
    path = record.get('supervisor_stderr_path')
    if path:
        try:
            with open(str(path) + '.pid') as stream:
                supervisor = int(stream.read())
        except (OSError, ValueError):
            supervisor = None
        if supervisor is not None:
            if alive(supervisor, record.get('supervisor_pid_start')):
                return False
            proof = True
    return proof


def run_stale_reason(record):
    """Why a non-terminal record is dead weight (its processes are gone), else None."""
    from .runner import is_terminal
    status = record.get('status')
    if (status in (None, 'queued', 'timed_out', 'stalled') or is_terminal(status) or record.get('ended') is not None
            or record.get('overflow_remote') or record.get('survivors')):
        return None
    return 'recorded pid(s) of the run are gone; the record was never ended' if run_pids_dead(record) else None


def with_stale(record):
    """The record as shown by ps/status: a queued run without a live owner, or a running record whose
    processes are gone, is marked stale with the reason."""
    reason = queue_stale_reason(record) or run_stale_reason(record)
    return dict(record, stale=True, stale_reason=reason) if reason else record


# The harness a run actually ran in, written onto the run record at launch.  Record fields:
# ``harness`` (from the launched leg, not the requested one), ``harness_version``
# (``<client> --version``; ``unknown`` when unreadable, never guessed), ``harness_fallback``
# (why the model's own harness was not used) and ``host``.  ``harness_report`` lists runs,
# failures and no-verdict reviews per harness.
UNKNOWN_HARNESS = "unknown"
_VERSION_TIMEOUT_S = 10


@lru_cache(maxsize=None)
def client_version(name: str) -> str:
    """First line of ``<name> --version`` or ``unknown``."""
    path = shutil.which(name)
    if not path:
        return UNKNOWN_HARNESS
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=_VERSION_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_HARNESS
    lines = (out.stdout or out.stderr).strip().splitlines() if out.returncode == 0 else []
    return lines[0].strip()[:200] if lines and lines[0].strip() else UNKNOWN_HARNESS


def _host_name() -> str:
    """The host identity the run records use (PLANGRAPH_HOST, as the cooldown lookup reads it)."""
    return os.environ.get("PLANGRAPH_HOST") or os.environ.get("PLANGRAPH_HOST_NAME") or socket.gethostname()


def harness_fields(harness: str, fallback: str | None, host: str | None = None) -> dict:
    return {"harness": harness or UNKNOWN_HARNESS, "harness_version": client_version(harness) if harness else UNKNOWN_HARNESS,
            "harness_fallback": fallback, "host": host or _host_name()}


def stamp_harness(record: dict, leg, host: str | None = None) -> None:
    """Write the launched leg's harness fields onto ``record``; a host the record already names stays."""
    fields = harness_fields(getattr(leg, "harness", None), getattr(leg, "harness_fallback", None), host)
    record.update({**fields, "host": record.get("host") or fields["host"]})


def native_harness_fields(host: str | None = None) -> dict:
    """Fields of a native Codex writer run."""
    return harness_fields("codex", None, host)


def harness_report(runs, *, now: float | None = None, hours: float = 24) -> dict:
    """Runs, failed runs and no-verdict reviews of the last ``hours`` hours, per harness."""
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    counts: dict[str, int] = {}
    failed: dict[str, list] = {}
    no_verdict: dict[str, list] = {}
    for run in runs:
        when = run.get("ended") or run.get("started") or 0
        if when < cutoff:
            continue
        harness = run.get("harness") or UNKNOWN_HARNESS
        counts[harness] = counts.get(harness, 0) + 1
        if run.get("status") in ("failed", "refused", "timeout"):
            failed.setdefault(harness, []).append(run["run_id"])
        if run.get("role") == "review" and run.get("reason") == "no_verdict":
            no_verdict.setdefault(harness, []).append(run["run_id"])
    return {"hours": hours, "runs": counts, "failed": failed, "no_verdict": no_verdict}
