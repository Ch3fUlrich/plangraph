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


def is_opencode(job):
    from pathlib import Path
    command = getattr(job, 'command', ()) if job is not None else ()
    return bool(command and Path(command[0]).name == 'opencode')


def failure_reason(job, stop=None):
    text = without_progress(job.stderr) if is_opencode(job) else job.stderr
    return first_reason(text) or ('provider_stop' if stop else f'harness exit code {job.exit_code}')


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


def fallthrough_reason(service, chosen, job, record):
    from .question_bridge import question_exit
    if question_exit(job, record):
        return None
    if record.get('reason') == 'model_refusal' or any(e.get('event') == 'model_refusal' for e in getattr(job, 'events', ())):
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
    line = harness_limit_line(job.exit_code, *(map(without_progress, streams) if is_opencode(job) else streams))
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
                    if e.get('event') in ('git_config_mutated', 'writer_no_changes')), None)
    if special is not None:
        record['reason'] = special
        return
    real = [a for a in record.get('attempts', ()) if a.get('status') != 'skipped']
    leg = real[-1].get('leg') if real else None
    leg = leg if isinstance(leg, dict) else {}
    opencode = is_opencode(job) or (record.get('chosen_leg') or leg).get('harness') == 'opencode'
    if opencode:
        for field in ('reason', 'error'):
            if record.get(field):
                record[field] = without_progress(record[field])
    reason = record.get('reason')
    if reason in TERMINAL_REASONS:
        return
    if record.get('status') in ('failed', 'refused'):
        # Only spawner-owned pre-exec errors/events, never harness stderr echoes.
        errors = (record.get('error') or '').split('; ')
        events = [*events, *getattr(job, 'events', ())]
        for named in ('prompt_too_large', 'stdin_prompt_flag_missing', 'stdin_prompt_command_unknown', 'stdin_prompt_argv_conflict'):
            if any(error.removeprefix('inline_diff_failed: ').startswith(named + ':') for error in errors) or any(
                    event.get('event') == 'prompt_transport_refused' and event.get('reason') == named for event in events):
                record['reason'] = named
                return
    if (record.get('role') == 'review' and record.get('status') == 'completed'
            and record.get('exit_code') in (None, 0)):
        from plangraph.review.verdict import judge
        # Base dispatch has no job here: use only the final attempt, never the
        # combined stdout tail (an earlier leg's verdict cannot bless this one).
        attempts = [a for a in record.get('attempts') or () if a.get('status') != 'skipped']
        text = job.stdout if job is not None else (attempts[-1].get('stdout_tail', '') if attempts else '')
        if judge(text, getattr(job, 'response_finish_reason', None)).verdict is None:
            record.update(status='failed', reason='no_verdict')
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
            elif not (last.get('skip_reason') or '').startswith('harness_limit'):
                reason = last.get('skip_reason') or reason
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
        record['reason'] = (reason or record.get('error')
                            or first_reason(stderr)
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
