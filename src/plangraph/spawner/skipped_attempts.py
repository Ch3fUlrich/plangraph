"""Fill a finished run record with an explicit attempt for every leg that never ran."""


def fill_skipped_attempts(record):
    """Append a ``skipped`` attempt for each planned or skipped leg with no attempt yet."""
    if record.get('ended') is None:
        return
    attempted = {a['leg'].get('id') if isinstance(a.get('leg'), dict) else a.get('leg')
                 for a in record.get('attempts', [])}
    plan = record.get('plan') or {}
    for candidate in plan.get('skipped', []):
        leg = candidate.get('leg')
        if leg not in attempted:
            record.setdefault('attempts', []).append(dict(leg=leg, status='skipped',
                down_reason='not_served_on_host' if (candidate.get('reason') or '').split(':', 1)[0].strip() == 'not_served_on_host' else None,
                skip_reason=candidate.get('skip_reason') or candidate.get('reason'),
                served_model='unwitnessed', served_family='unwitnessed',
                served_witness_reason='leg skipped before launch'))
            attempted.add(leg)
    for leg in plan.get('legs', []):
        if leg['id'] not in attempted:
            record.setdefault('attempts', []).append(dict(leg=leg, status='skipped',
                skip_reason='earlier_leg_succeeded' if record.get('status') in ('completed', 'dry_run') else 'run_terminated_before_leg',
                served_model='unwitnessed', served_family='unwitnessed',
                served_witness_reason='leg not launched'))
