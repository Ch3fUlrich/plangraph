"""Shared dispatch outcome policy for base and routed launches."""
from .env_redact import first_reason, harness_limit_line, unavailable_reason

#: Verdicts a guard or a cancel owns.  No later fallthrough outcome may stomp
#: them and terminal_reason never pops them (M9/M15, Q14).
TERMINAL_REASONS = ('git_config_mutated', 'writer_no_changes', 'cancelled')


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


def terminal_reason(record):
    """Q8/M15: the run's reason describes how the run ENDED.

    A 'harness_limit' stamped by an earlier fallthrough leg never survives a
    later attempt's outcome.  Guard evidence names the run (M9), and guard and
    cancel verdicts are terminal: never stomped, never popped (M15, Q14).
    """
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
