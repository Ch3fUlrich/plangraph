"""Shared dispatch outcome policy for base and routed launches."""
import atexit

from plangraph.routing_support.leg_state import _down_seconds
from .env_redact import first_reason, harness_limit_line, unavailable_reason

#: Verdicts a guard or a cancel owns.  No later fallthrough outcome may stomp
#: them and terminal_reason never pops them (M9/M15, Q14).
TERMINAL_REASONS = ('git_config_mutated', 'writer_no_changes', 'cancelled')


def without_progress(text):
    """OpenCode tool diagnostics are not failure evidence (including ANSI output)."""
    from .env_redact import normalize
    return '\n'.join(line for line in text.splitlines()
                     if not normalize(line).lstrip().startswith(('✱', '→', '>', '$')))


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


def _book_failure(service, chosen, line):
    """Book legacy evidence and quarantine the enabled reader's cached identity.

    The adapter replaces its served set on authoritative refresh, so this
    observation is transient, not a second availability catalog. Read the
    injected adapter flag only; disabled readers retain legacy store semantics.
    """
    from plangraph.routing_support.leg_state import LegAdapter
    from .leg_probe import key
    target = _limit_target(service)
    target.record_harness_limit(key(chosen), line, 'spawner:harness', down_s=_booking_seconds())
    if isinstance(target, LegAdapter) and target.enabled:
        target.quarantine(key(chosen))


def fallthrough_reason(service, chosen, job, record):
    if record.get('reason') == 'model_refusal' or any(e.get('event') == 'model_refusal' for e in getattr(job, 'events', ())):
        return None   # safety stops are not provider limits or leg-health evidence
    line = harness_limit_line(job.exit_code, job.stdout, job.stderr)
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
        _book_failure(service, chosen, line)
        # M9/M15: a guard or cancel verdict is never stomped by a fallthrough.
        if record.get('reason') not in TERMINAL_REASONS:
            record['reason'] = 'harness_limit'
        reason = 'harness_limit: ' + line
        notify_leg_refused(service, record, chosen, reason)
        return reason
    reason = unavailable_reason(job.exit_code, job.stdout, job.stderr)
    if reason is not None:
        _book_failure(service, chosen, 'model_unavailable: ' + reason)
        notify_leg_refused(service, record, chosen, reason)
    return reason


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


def terminal_reason(record):
    """Q8/M15: the run's reason describes how the run ENDED.

    A 'harness_limit' stamped by an earlier fallthrough leg never survives a
    later attempt's outcome.  Guard evidence names the run (M9), and guard and
    cancel verdicts are terminal: never stomped, never popped (M15, Q14).
    """
    if record.get('reason') == 'kill_incomplete':
        return   # survivors outrank every other verdict: the run failed
    events = record.get('events') or ()
    special = next((e['event'] for e in events
                    if e.get('event') in ('git_config_mutated', 'writer_no_changes')), None)
    if special is not None:
        record['reason'] = special
        return
    reason = record.get('reason')
    if reason in TERMINAL_REASONS:
        return
    ended_ok = record.get('exit_code') in (None, 0) and record.get('status') in ('completed', 'dry_run')
    if reason == 'harness_limit':
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
            elif not (last.get('skip_reason') or '').startswith('harness_limit'):
                reason = last.get('skip_reason') or reason
        record['reason'] = reason
        return
    if record.get('exit_code') not in (None, 0):
        record['reason'] = (reason or record.get('error')
                            or first_reason(record.get('stderr_tail', ''))
                            or f"harness exit {record['exit_code']}")


def orphaned(record, now):
    """Q21/M14: a 'running' record whose supervisor process is gone ends 'failed', reason 'orphaned'."""
    import os
    path = record.get('supervisor_stderr_path')
    # os.kill(pid, 0) is TerminateProcess on Windows: never probe there.
    if record.get('status') != 'running' or record.get('ended') is not None or not path or os.name == 'nt':
        return None
    pid = None
    try:
        pid = int(open(str(path) + '.pid').read())
        os.kill(pid, 0)
        return None
    except PermissionError:
        return None   # alive, owned by another user
    except (OSError, ValueError):
        pass
    if not os.path.exists(str(path) + '.pid'):
        return None   # no pid evidence: not proof the supervisor died
    return dict(record, status='failed', exit_code=1, ended=now, reason='orphaned',
                error=f'orphaned: supervisor pid {pid} is gone')
