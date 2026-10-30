"""Shared dispatch outcome policy for base and routed launches."""
from .env_redact import first_reason, harness_limit_line, unavailable_reason


def fallthrough_reason(service, chosen, job, record):
    from . import leg_state
    line = harness_limit_line(job.exit_code, job.stdout, job.stderr)
    if line:
        target = service.leg_reader
        if not hasattr(target, 'record_harness_limit'):
            target = getattr(target, '__self__', None)
        if not hasattr(target, 'record_harness_limit'):
            target = leg_state
        model = chosen.model if '/' in chosen.model else 'agy/' + chosen.model
        target.record_harness_limit(model, line, 'spawner:harness')
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
