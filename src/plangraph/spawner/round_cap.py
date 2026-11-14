"""Round cap (D-5813, rule 3.35): no 4th implement spawn per keeper item; request an L1 replan instead."""
from contextvars import ContextVar
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
_LAUNCH = ContextVar("round_launch", default=None)
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
    diff = card.get('diff_ref') if isinstance(card, dict) else None
    return str(diff['head']) if isinstance(diff, dict) and diff.get('head') else None


def work_refs(card):
    """Keep PR and branch independently, including the diff's actual head branch."""
    if not isinstance(card, dict):
        return []
    refs = {f'pr:{card[k]}' for k in ('pr', 'pr_number') if card.get(k) not in (None, '')}
    diff = card.get('diff_ref')
    branches = [card.get('branch'), diff.get('head') if isinstance(diff, dict) else None]
    refs.update(f'branch:{b}' for b in branches if b)
    return sorted(refs)


def matches_work(record, item, refs):
    if record.get('item_key') == item:
        return True
    if set(record.get('round_refs') or ()) & set(refs):
        return True
    # Older records stored only one untyped PR/branch reference.
    return (not record.get('round_refs') and record.get('round_ref') is not None
            and str(record['round_ref']) in {r.split(':', 1)[1] for r in refs})


def prior_rounds(runs, item, ref):
    """Count launched rounds matching the item OR any PR/branch work identity."""
    refs = ref if isinstance(ref, list) else ([f'pr:{ref}', f'branch:{ref}'] if ref else [])
    return sum(1 for r in runs
               if r.get('role') == 'implement' and matches_work(r, item, refs)
               and r.get('status') not in ('refused', 'dry_run')
               and not r.get('continuation') and writer_started(r))


def require_keeper_claim(card):
    """Bind admission to a live keeper claim held by the authenticated identity.

    Use the existing keeper read API, with no snapshot fallback: a caller's
    item label or claimed_by field is never proof of a claim.
    """
    from plangraph.keeper.cli_verbs import Session, RefusedCli, Unreachable
    from .item_key import annotate_item
    identity = {}
    annotate_item(identity, card)
    key = identity['item_key']
    try:
        session = Session()
        item = session.call('GET', 'show', dict(key=key))
        who = session.call('GET', 'whoami')
    except (RefusedCli, Unreachable) as exc:
        raise SpawnRefused('keeper claim unavailable or refused; implement admission fails closed') from exc
    if (not isinstance(item, dict) or not isinstance(who, dict) or item.get('key') != key
            or item.get('state') != 'claimed' or not who.get('name')
            or item.get('claimed_by') != who['name']
            or not isinstance(item.get('lease_until'), (int, float))
            or not item['lease_until'] > time.time()):
        raise SpawnRefused('keeper claim required: existing item with a live lease held by the authenticated caller')


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
    """(payload, route plan) once the item has used its rounds, else None."""
    from .item_key import annotate_item
    identity = {}
    annotate_item(identity, card)
    item = identity['item_key']
    if role != 'implement':
        return None
    if not item:
        raise SpawnRefused('item_key required for implement round accounting')
    from .stall_watchdog import read_runs
    ref = round_ref(card)
    done = prior_rounds(read_runs(service.state), item, work_refs(card)) if rounds is None else rounds
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
        item=item, pr=ref, rounds=done,
        binding='keeper item OR PR/branch; local cap checked before live keeper claim (also when keeper unavailable)',
        max_rounds=MAX_ROUNDS, findings=findings,
        findings_source='card.findings' if findings else 'none (the card carries no findings)',
        replan_leg=plan.first_leg.id if plan else None,
        replan_leg_order=[leg.id for leg in plan.legs] if plan else [],
        replan_route_error=route_error, replan_card=replan_card,
        automatic_replan=dict(status='deferred', reason=(
            'Missing item-named stronger model and durable research dispatch/receipt ownership; '
            'the current synchronous alarm path only requests L1 work.'))), plan


def check_round_cap(service, card, role):
    """Refuse without side effects (counting only)."""
    found = round_cap_payload(service, card, role)
    if found:
        raise RoundCapRefused(found[0])


async def enforce_round_cap(service, card, role, *, dry_run=False):
    """Refuse the capped spawn and request an L1 replan (once per item)."""
    found = round_cap_payload(service, card, role, rounds=0 if dry_run else None)
    if found is None:
        if role == 'implement' and not dry_run:
            import asyncio
            await asyncio.to_thread(require_keeper_claim, card)
        return
    payload, plan = found
    import asyncio
    payload['replan'] = await asyncio.to_thread(start_replan, service, payload, plan)
    raise RoundCapRefused(payload)


def marker_path(service, item, ref):
    digest = hashlib.sha256(item.encode()).hexdigest()[:16]
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
    refs = record.get('round_refs') or ([f'pr:{ref}', f'branch:{ref}'] if ref else [])
    # One admission lock covers every overlapping item/PR/branch, including
    # different item files. Keep the old item files readable across upgrades.
    with inbox_lock(path.parent / 'admission'):
        entries = json.loads(path.read_text()) if path.exists() else {}
        launched = {r['run_id'] for r in read_runs(service.state) if prior_rounds([r], item, refs)}
        if run_id in launched:
            return None
        pending = set()
        for saved in path.parent.glob('*.json'):
            for ident, entry in json.loads(saved.read_text()).items():
                if saved == path or matches_work(entry, item, refs):
                    pending.add(ident)
        if run_id not in pending and len(launched | pending) >= MAX_ROUNDS:
            return False
        entries.setdefault(run_id, dict(launched=False, item_key=item, round_refs=refs))
        atomic_write_private(path, json.dumps(entries))
    return path, run_id


def finish_round(token, launched):
    from .notify import inbox_lock
    from .private_io import atomic_write_private
    if token is None:
        return
    path, run_id = token
    with inbox_lock(path.parent / 'admission'):
        entries = json.loads(path.read_text())
        if launched:
            entries[run_id]['launched'] = True
        elif launched is False and not entries[run_id]['launched']:
            del entries[run_id]
        atomic_write_private(path, json.dumps(entries))


def launch_phase(phase):
    """Runner declares a proven prelaunch failure boundary before creating a process."""
    if (state := _LAUNCH.get()) is not None:
        state['phase'] = phase


def observe_process(job_id, proc):
    """Record the first PID before identity checks, monitoring or any further await."""
    if (state := _LAUNCH.get()) is not None and getattr(proc, 'pid', None):
        record = state['record']
        record.update(writer_launched=True, writer_launch_pid=proc.pid)
        state['phase'] = 'launched'
        finish_round(state['token'], True)
        if state['prompt_record'] is not None:
            state['prompt_record'][2](record)


async def spawn_round(service, record, card, *, prompt_record, **options):
    """Reserve before spawn; release only a proven prelaunch refusal/failure."""
    import asyncio
    from .item_key import valid_item
    if record.get('role') == 'implement' and not valid_item(record.get('item_key')):
        raise SpawnRefused('item_key required for implement round accounting')
    if (record.get('role') != 'implement' or
            record.get('continuation') or options.get('dry_run')):
        return await service.runner.spawn(prompt_record=prompt_record, **options)
    token = await asyncio.to_thread(reserve_round, service, record)
    if token is False:
        card = dict(card or {}, item_key=record['item_key'], pr=record.get('round_ref'))
        payload, plan = round_cap_payload(service, card, 'implement', rounds=MAX_ROUNDS)
        payload['replan'] = await asyncio.to_thread(start_replan, service, payload, plan)
        raise RoundCapRefused(payload)
    state = dict(phase='unknown', record=record, token=token, prompt_record=prompt_record)
    context = _LAUNCH.set(state)
    try:
        job = await service.runner.spawn(prompt_record=prompt_record, **options)
        process = getattr(service.runner, '_processes', {}).get(job.job_id)
        pid = getattr(process, 'pid', None) or getattr(service.runner, '_owned', {}).get(job.job_id, {}).get('pid')
        if pid and state['phase'] != 'launched':
            from types import SimpleNamespace
            observe_process(job.job_id, SimpleNamespace(pid=pid))
        return job
    finally:
        _LAUNCH.reset(context)
        launched = True if record.get('writer_launched') else False if state['phase'] == 'prelaunch' else None
        if launched is False:
            record.setdefault('writer_launched', False)
        await asyncio.shield(asyncio.to_thread(finish_round, token, launched))
