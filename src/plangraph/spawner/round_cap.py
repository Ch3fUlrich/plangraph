"""Round cap (D-5813, rule 3.35): no 4th implement spawn per item and PR; replan instead."""
import hashlib
import json
import time
import uuid
from pathlib import Path

from plangraph.routing_errors import SpawnRefused

def writer_started(run):
    """Positive launch evidence; a limited admission record alone spends no round."""
    if 'writer_launched' in run:
        return run['writer_launched'] is True
    return bool(run.get('pid') or (run.get('job_id') and run.get('status') != 'limited') or any(
        isinstance(a, dict) and a.get('status') in ('running', 'completed', 'failed', 'stalled', 'timeout',
        'timed_out', 'cancelled', 'terminated_after_answer') for a in (run.get('attempts') or ())))


MAX_ROUNDS = 3
STALE_REPLAN_S = 3600


class RoundCapRefused(SpawnRefused):
    reason = 'round_cap'

    def __init__(self, payload):
        super().__init__(json.dumps(dict(reason=self.reason, replan_required=payload), sort_keys=True))
        self.payload = payload


def round_ref(card):
    """PR number or branch the card works on, or None."""
    for key in ('pr', 'pr_number', 'branch'):
        value = card.get(key) if isinstance(card, dict) else None
        if value not in (None, ''):
            return str(value)
    return None


def prior_rounds(runs, item, ref):
    """Count real implement runs of one (item, ref); a run without a recorded ref
    belongs to the ref-less bucket and counts only for a card without a ref.
    Refusals, dry runs and same-round continuations are not rounds."""
    return sum(1 for r in runs
               if r.get('role') == 'implement' and r.get('item_key') == item
               and r.get('status') not in ('refused', 'dry_run')
               and not r.get('continuation') and writer_started(r)
               and r.get('round_ref') == ref)


def finding_texts(card):
    """The card's open findings as verbatim strings."""
    texts = []
    for finding in card.get('findings') or ():
        if isinstance(finding, dict):
            finding = finding.get('text') or finding.get('description') or json.dumps(finding, sort_keys=True)
        texts.append(str(finding))
    return texts


def replan_route(service, replan_card):
    """(plan, None) from the route table for role research, or (None, named error).
    Only a routing refusal is an expected failure; any other error surfaces."""
    try:
        plan = service._route(replan_card, 'research')
    except SpawnRefused as exc:
        return None, str(exc)
    if plan.error or not plan.first_leg:
        return None, plan.error or 'no eligible planning leg'
    return plan, None


def round_cap_payload(service, card, role, *, rounds=None):
    """(payload, route plan) once the item/PR has used its rounds, else None."""
    from .item_key import annotate_item
    identity = {}
    annotate_item(identity, card)
    item = identity['item_key']
    if role != 'implement' or not item:
        return None
    from .stall_watchdog import read_runs
    ref = round_ref(card)
    done = prior_rounds(read_runs(service.state), item, ref) if rounds is None else rounds
    if done < MAX_ROUNDS:
        return None
    findings = finding_texts(card)
    replan_card = dict({k: v for k, v in card.items() if k not in ('findings', 'role')},
                       role='research', item_key=item, pr=ref,
                       task=f'Replan {item}: {done} implement rounds did not merge. Read the PR, the item and '
                            'the open findings, close the PR, define slices small enough for one round each. '
                            'Do not implement.')
    plan, route_error = replan_route(service, replan_card)
    return dict(
        item=item, pr=ref, rounds=done, max_rounds=MAX_ROUNDS, findings=findings,
        findings_source='card.findings' if findings else 'none (the card carries no findings)',
        replan_leg=plan.first_leg.id if plan else None,
        replan_leg_order=[leg.id for leg in plan.legs] if plan else [],
        replan_route_error=route_error, replan_card=replan_card), plan


def check_round_cap(service, card, role):
    """Refuse without side effects (counting only)."""
    found = round_cap_payload(service, card, role)
    if found:
        raise RoundCapRefused(found[0])


async def enforce_round_cap(service, card, role, *, dry_run=False):
    """Refuse the capped spawn and request an L1 replan (once per item/PR)."""
    found = None if dry_run else round_cap_payload(service, card, role)
    if found is None:
        return
    payload, plan = found
    import asyncio
    payload['replan'] = await asyncio.to_thread(start_replan, service, payload, plan)
    raise RoundCapRefused(payload)


def marker_path(service, item, ref):
    digest = hashlib.sha256(f'{item}\0{ref}'.encode()).hexdigest()[:16]
    return Path(service.state) / 'replan' / (digest + '.json')


def start_replan(service, payload, plan):
    """Persist independent keeper/outbox receipts; retries retain their request identity."""
    from .private_io import atomic_write_private
    from .replan_job import request_replan
    from .notify import inbox_lock
    path = marker_path(service, payload['item'], payload['pr'])
    with inbox_lock(path):
        record = json.loads(path.read_text()) if path.exists() else dict(
            item=payload['item'], ref=payload['pr'], rounds=payload['rounds'], request=payload,
            status='new', run_id='replan-' + uuid.uuid4().hex)
        if record['status'] == 'requested' or (record['status'] == 'running' and
                time.time() - record.get('started', 0) <= STALE_REPLAN_S):
            return dict(status=record['status'], run_id=record['run_id'], started=False)
        record.update(status='running', started=time.time())
        save = lambda: atomic_write_private(path, json.dumps(record))
        save()
        try:
            request_replan(service, record['request'], record['run_id'], path, record, save)
            record.update(status='requested', reason=None)
        except Exception as exc:
            record.update(status='failed', reason=type(exc).__name__)
        save()
        return dict(status=record['status'], run_id=record['run_id'], reason=record.get('reason'), started=False)


def reserve_round(service, record):
    """Atomically reserve the final launch capacity across independent supervisors."""
    from .notify import inbox_lock
    from .private_io import atomic_write_private
    from .stall_watchdog import read_runs
    item, ref, run_id = record['item_key'], record.get('round_ref'), record['run_id']
    path = Path(service.state) / 'rounds' / marker_path(service, item, ref).name
    with inbox_lock(path):
        entries = json.loads(path.read_text()) if path.exists() else {}
        launched = {r['run_id'] for r in read_runs(service.state) if prior_rounds([r], item, ref)}
        if run_id in launched:
            return None
        if run_id not in entries and len(launched | entries.keys()) >= MAX_ROUNDS:
            return False
        entries.setdefault(run_id, dict(launched=False))
        atomic_write_private(path, json.dumps(entries))
    return path, run_id


def finish_round(token, launched):
    from .notify import inbox_lock
    from .private_io import atomic_write_private
    if token is None:
        return
    path, run_id = token
    with inbox_lock(path):
        entries = json.loads(path.read_text())
        if launched:
            entries[run_id]['launched'] = True
        elif not entries[run_id]['launched']:
            del entries[run_id]
        atomic_write_private(path, json.dumps(entries))


async def spawn_round(service, record, card, *, prompt_record, **options):
    """Launch seam: reserve before spawn, retain only positive process evidence."""
    import asyncio
    if (record.get('role') != 'implement' or not record.get('item_key') or
            record.get('continuation') or options.get('dry_run')):
        return await service.runner.spawn(prompt_record=prompt_record, **options)
    token = await asyncio.to_thread(reserve_round, service, record)
    if token is False:
        card = dict(card or {}, item_key=record['item_key'], pr=record.get('round_ref'))
        payload, plan = round_cap_payload(service, card, 'implement', rounds=MAX_ROUNDS)
        payload['replan'] = await asyncio.to_thread(start_replan, service, payload, plan)
        raise RoundCapRefused(payload)
    record.setdefault('writer_launched', False)
    try:
        job = await service.runner.spawn(prompt_record=prompt_record, **options)
        if (service.runner._processes.get(job.job_id) is not None or
                getattr(service.runner, '_owned', {}).get(job.job_id, {}).get('pid')):
            record['writer_launched'] = True
        return job
    finally:
        await asyncio.shield(asyncio.to_thread(finish_round, token, record['writer_launched']))
