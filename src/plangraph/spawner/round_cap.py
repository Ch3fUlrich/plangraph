"""Round cap (D-5813, rule 3.35): no 4th implement spawn per item and PR; replan instead."""
import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from plangraph.routing_errors import SpawnRefused

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
               and not r.get('continuation')
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


def round_cap_payload(service, card, role):
    """(payload, route plan) once the item/PR has used its rounds, else None."""
    item = card.get('item_key') or card.get('item') if isinstance(card, dict) else None
    if role != 'implement' or not item:
        return None
    from .stall_watchdog import read_runs
    ref = round_ref(card)
    done = prior_rounds(read_runs(service.state), item, ref)
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
    """Refuse the capped spawn and start the automatic replan job (once per item/PR)."""
    found = None if dry_run else round_cap_payload(service, card, role)
    if found is None:
        return
    payload, plan = found
    payload['replan'] = start_replan(service, payload, plan)
    raise RoundCapRefused(payload)


def marker_path(service, item, ref):
    digest = hashlib.sha256(f'{item}\0{ref}'.encode()).hexdigest()[:16]
    return Path(service.state) / 'replan' / (digest + '.json')


def start_replan(service, payload, plan):
    """Claim the (item, PR) marker and schedule the replan job as a service task."""
    from .private_io import atomic_write_private
    from .replan_job import default_io, run_marked
    io = getattr(service, 'replan_io', None) or default_io(service)
    item, ref = payload['item'], payload['pr']
    if plan is None:
        line = (f"BLOCKED {item} | round_cap replan not started: no planning leg ({payload['replan_route_error']}) "
                "| fix the route, then re-spawn | -")
        io.notify(item, None, line)
        return dict(status='blocked', reason=payload['replan_route_error'], started=False)
    path = marker_path(service, item, ref)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = dict(item=item, ref=ref, rounds=payload['rounds'], status='running',
                  run_id='replan-' + uuid.uuid4().hex, started=time.time())
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                prior = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                prior = dict(status='unreadable')
            if prior.get('status') == 'running' and time.time() - prior.get('started', 0) > STALE_REPLAN_S:
                path.unlink(missing_ok=True)
                continue
            return dict(status=prior.get('status'), run_id=prior.get('run_id'),
                        reason=prior.get('reason'), started=False)
        os.close(fd)
        break
    else:
        return dict(status='unreadable', started=False)
    atomic_write_private(path, json.dumps(record))
    save = lambda: atomic_write_private(path, json.dumps(record))
    task = asyncio.create_task(run_marked(service, payload, plan, io, record, save, path.with_suffix('.items.json')))
    service.tasks[record['run_id']] = task
    return dict(status='running', run_id=record['run_id'], started=True)
