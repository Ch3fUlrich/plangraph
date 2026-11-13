"""Resolved review-slot metadata and fail-closed response-side evidence publication."""
import json
import re
from pathlib import Path

from plangraph.review.verdict import judge
from .private_io import atomic_write_private


def publish(directory, record, job):
    """Publish evidence last; never recover evidence from combined run tails."""
    if record.get('role') != 'review' or record.get('ended') is None:
        return
    if job is None or record.get('job_id') not in (None, job.job_id):
        return  # retries without the owning final job cannot invalidate evidence
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('symlink run directory')
    review, evidence = directory / 'review.txt', directory / 'evidence.json'
    def clear():
        evidence.unlink(missing_ok=True)
        review.unlink(missing_ok=True)
    clear()  # unlink entries, never open a stale pair (including symlinks)
    if (job is None or record.get('status') != 'completed' or record.get('exit_code') != 0
            or record.get('job_id') not in (None, job.job_id) or job.status != 'completed'
            or job.exit_code != 0):
        return
    model = getattr(job, 'served_model', None)
    digest = getattr(job, 'prompt_sha256', None)
    finish = getattr(job, 'response_finish_reason', None)
    if (not model or model == 'unwitnessed' or finish not in ('stop', 'end_turn')
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        return
    verdict = judge(job.stdout, finish)
    if verdict.verdict is None or verdict.test_gaming == 'missing':
        return
    if verdict.verdict == 'pass' and verdict.test_gaming != 'no':
        return
    doc = dict(served_model=model, prompt_sha256=digest,
               correlation_id=job.job_id, run_id=record['run_id'],
               witness_kind='response-side', **verdict.to_evidence())
    doc.update(finish_reason=finish)
    try:
        atomic_write_private(review, job.stdout)
        atomic_write_private(evidence, json.dumps(doc, indent=2))
    except BaseException:
        clear()
        raise


def seat_reasons(families, reasons):
    from plangraph.planner.seats import seat_family
    reasons = list(reasons)
    if len(families) > 1 and seat_family(families[1]) == 'anthropic':
        reasons.append('second review family is anthropic')
    return tuple(dict.fromkeys(reasons))


def sync_seat(record):
    """Keep the slot's audit metadata tied to the resolved leg on every save."""
    from plangraph.planner.seats import seat_family
    plan = record.get('plan') or {}
    own = plan.get('seat_coverage') or []
    if len(own) != 1 or not own[0].get('seat_group'):
        return
    chosen = record.get('chosen_leg')
    if chosen:
        plan['review_seats'] = [dict(chosen)]
        own[0].update(leg=chosen['id'], family=chosen['family'],
                      keeper_family=seat_family(chosen['family']))
        own[0].pop('reason', None)
    elif record.get('ended') is not None:
        plan['review_seats'] = []
        detail = '; '.join(dict.fromkeys(a['skip_reason'] for a in record.get('attempts', [])
                                        if a.get('skip_reason')))
        reason = 'distinct_family_unavailable: no eligible leg of a family not held by another seat'
        reason += ': ' + (detail or record.get('error') or 'no leg launched')
        reason = own[0].get('reason') or reason
        own[0].update(family=None, keeper_family=None, reason=reason)
        if record.get('status') != 'refused':
            record['error'] = reason
