"""Shared dispatch outcome policy for base and routed launches."""
import re

from .env_redact import first_reason, harness_limit_line, unavailable_reason

#: Verdicts a guard or a cancel owns.  No later fallthrough outcome may stomp
#: them and terminal_reason never pops them (M9/M15, Q14).
TERMINAL_REASONS = ('git_config_mutated', 'writer_no_changes', 'no_verdict', 'cancelled')


def fallthrough_reason(service, chosen, job, record):
    from . import leg_state
    from .leg_probe import key as leg_key
    line = harness_limit_line(job.exit_code, job.stdout, job.stderr)
    if line:
        target = service.leg_reader
        if not hasattr(target, 'record_harness_limit'):
            target = getattr(target, '__self__', None)
        if not hasattr(target, 'record_harness_limit'):
            target = leg_state
        # Q16/M8: one leg key shared with the probe path -- 'agy/<model>' only
        # for an agy leg's bare model.
        target.record_harness_limit(leg_key(chosen), line, 'spawner:harness')
        # M9/M15: a guard or cancel verdict is never stomped by a fallthrough.
        if record.get('reason') not in TERMINAL_REASONS:
            record['reason'] = 'harness_limit'
        return 'harness_limit: ' + line
    return unavailable_reason(job.exit_code, job.stdout, job.stderr)


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
    events = record.get('events') or ()
    special = next((e['event'] for e in events
                    if e.get('event') in ('git_config_mutated', 'writer_no_changes', 'no_verdict')), None)
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
            if last.get('status') == 'timeout':
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


def enforce_verdict(role, job):
    """A review seat's verdict is its stdout (D-3226): no VERDICT line = failed/no_verdict."""
    if (role == 'review' and job.status == 'completed'
            and not re.search(r'^\s*VERDICT:\s*(PASS|FAIL)', job.stdout or '', re.M)):
        job.status = 'failed'
        job.exit_code = 1
        job.events = getattr(job, 'events', None) or []
        job.events.append({'event': 'no_verdict'})
        job.stderr = 'no_verdict: review run produced no VERDICT: PASS|FAIL line\n' + (job.stderr or '')


def check_review_model(role, target):
    """D-3225 refuse a no-verdict seat model; D-3422 warn on a review-unfit one."""
    from plangraph.planner.legs import NO_VERDICT_SEATS, REVIEW_WARN_SEATS
    from plangraph.planner.routing import SpawnRefused
    if role != 'review':
        return
    if target in NO_VERDICT_SEATS:
        raise SpawnRefused('no-verdict seat model (D-3225): ' + target)
    if target in REVIEW_WARN_SEATS:
        import logging
        logging.getLogger(__name__).warning('review-unfit seat model (D-3422): %s', target)
