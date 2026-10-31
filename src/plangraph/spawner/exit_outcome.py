"""Shared dispatch outcome policy for base and routed launches."""
from .env_redact import first_reason, harness_limit_line, unavailable_reason


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
        record['reason'] = 'harness_limit'
        return 'harness_limit: ' + line
    return unavailable_reason(job.exit_code, job.stdout, job.stderr)


def terminal_reason(record):
    if record.get('exit_code') not in (None, 0):
        events = record.get('events', [])
        special = next((e['event'] for e in events
                        if e.get('event') in ('git_config_mutated', 'writer_no_changes')), None)
        record['reason'] = (special or record.get('reason') or record.get('error')
                            or first_reason(record.get('stderr_tail', ''))
                            or f"harness exit {record['exit_code']}")
    else:
        record.pop('reason', None)
