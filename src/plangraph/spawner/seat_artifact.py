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
    from plangraph.routing_errors import SpawnRefused
    canonical = [seat_family(f) if f is not None else None for f in families]
    filled = [f for f in canonical if f is not None]
    if len(filled) != len(set(filled)):
        raise SpawnRefused('distinct_family_unavailable: duplicate resolved seat family')
    reasons = list(reasons)
    if len(canonical) > 1 and canonical[1] == 'anthropic':
        reasons.append('second review family is anthropic')
    return tuple(dict.fromkeys(reasons))


def reserve_seat_families(plan, writers):
    """Give each canonical spare family, with all its alternates, one owner."""
    from plangraph.planner.seats import seat_family, writer_parts
    from plangraph.routing_errors import SpawnRefused
    seat_families = {seat_family(seat.family) for seat in plan.review_seats}
    if not seat_families or len(seat_families) != len(plan.review_seats):
        raise SpawnRefused('two distinct review families unavailable: missing or duplicate seat family')
    writers = writer_parts(writers)
    if seat_families & writers:
        raise SpawnRefused('distinct_family_unavailable: review seat is a writer family')
    owners = {seat_family(seat.family): i for i, seat in enumerate(plan.review_seats)}
    spares, next_slot = [[] for _ in plan.review_seats], 0
    for spare in plan.legs:
        family = seat_family(spare.family)
        if family in writers or family in seat_families:
            continue
        if family not in owners:
            owners[family] = next_slot % len(plan.review_seats)
            next_slot += 1
        spares[owners[family]].append(spare)
    return spares, owners


def restrict_seat_plan(plan, slot, tried):
    """Re-route only within this slot's reservation; name its next actual leg."""
    from dataclasses import replace
    from plangraph.planner.seats import seat_family
    allowed = {leg.id for leg in slot.legs}
    legs = tuple(leg for leg in plan.legs if leg.id in allowed)
    chosen = next((leg for leg in legs if leg.id not in tried), None)
    coverage = [dict(row) for row in slot.seat_coverage]
    for row in coverage:
        row.update(leg=chosen.id if chosen else None, family=chosen.family if chosen else None,
                   keeper_family=seat_family(chosen.family) if chosen else None)
    return replace(plan, legs=legs, review_seats=(chosen,) if chosen else (), seat_coverage=tuple(coverage))


def sync_seat(record, held_families=()):
    """Keep the slot's audit metadata tied to the resolved leg on every save."""
    from plangraph.planner.seats import seat_family
    plan = record.get('plan') or {}
    own = [row for row in plan.get('seat_coverage') or []
           if isinstance(row, dict) and row.get('seat_group')]
    if len(own) != 1:
        return
    chosen = record.get('chosen_leg')
    if chosen:
        family = seat_family(chosen['family'])
        eligible = own[0].get('eligible_families')
        if family in held_families or (eligible is not None and family not in eligible):
            import time
            reason = 'distinct_family_unavailable: duplicate or unreserved resolved seat family'
            record.update(chosen_leg=None, status='refused', refusal='SpawnRefused', exit_code=2,
                          error=reason, ended=record.get('ended') or time.time())
            own[0]['reason'] = reason
            chosen = None
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


def sync_seat_group(records, save):
    """Check resolved identities again before publishing any group coverage."""
    from plangraph.planner.seats import seat_family
    held = set()
    for record in records:
        chosen = record.get('chosen_leg')
        sync_seat(record, held)
        if record.get('chosen_leg'):
            held.add(seat_family(record['chosen_leg']['family']))
        if chosen != record.get('chosen_leg'):
            save(record)
