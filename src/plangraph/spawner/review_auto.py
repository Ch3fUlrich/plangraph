"""Interim terminal-writer review driver; routing and posting have one owner each."""
import asyncio
import json
import os
import re
from dataclasses import replace
from pathlib import Path

from plangraph.planner.seats import seat_family, writer_parts
from .item_key import valid_item
from .service import SpawnService


async def supervise_tasks(service, record):
    """Drain dynamic review work without attributing another run's failure here."""
    own = {r['run_id'] for r in record.get('seats', [record])}
    while pending := [task for task in service.tasks.values() if not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)
    for run_id in tuple(own):
        source = service.result(run_id)
        own.update(set(source.get('review_runs', [])) - set(source.get('review_voided', [])))
    for key, task in service.tasks.items():
        if key in own or any(key.startswith('review-' + run_id + '-') for run_id in own):
            task.result()


def terminal(service, record):
    """Queue from the terminal persistence door, never from a session caller."""
    head = (record.get('completion') or {}).get('head_sha')
    if (record.get('role') != 'implement' or record.get('status') != 'completed'
            or record.get('exit_code') != 0 or record.get('ended') is None
            or not valid_item(record.get('item_key')) or not re.fullmatch('[0-9a-f]{40}', head or '')):
        return
    requested = getattr(service, '_review_requested', {})
    service._review_requested = requested
    key = record['run_id']
    if requested.get(key) == head:
        return
    requested[key] = head
    changed = getattr(service, '_review_changed', {})
    service._review_changed = changed
    voided = record.get('review_auto_status') == 'voided' and record.get('review_head') == head
    for (run_id, prior_head), event in changed.items():
        if ((run_id == key and prior_head != head) or (not voided and run_id != key
                and service.result(run_id).get('item_key') == record['item_key'])):
            event.set()
    if voided:
        return
    changed[key, head] = signal = asyncio.Event()
    async def run():
        locks = getattr(service, '_review_locks', {})
        service._review_locks = locks
        async with locks.setdefault(record['item_key'], asyncio.Lock()):
            writer = service.result(key)
            if writer['completion']['head_sha'] != head:
                return
            try:
                await reviews(service, writer, head, signal)
            except Exception as exc:
                writer = service.result(key)
                if writer['completion']['head_sha'] != head:
                    return
                writer.update(review_auto_status='blocked', review_auto_error=type(exc).__name__)
                SpawnService._save(service, writer)
                requested.pop(key, None)  # Transient failures may retry this exact head.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run())
    else:
        service.tasks['review-' + key + '-' + head] = loop.create_task(run())


async def reviews(service, writer, head, changed):
    """Consume the one routed leg list, bounded by its length; CHANGES stops it."""
    import sys
    tools = str(Path(__file__).resolve().parents[3] / 'tools')
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import seat_verdict as seat
    from plangraph.keeper.evidence import _ci_conclusion
    from plangraph.keeper.evidence_ci import CIAdapter
    keeper = service.keeper_sink
    if not getattr(keeper, 'url', None) or not os.environ.get('PG_REVIEWER_TOKEN_DIR'):
        raise ValueError('review posting host not configured')
    old_head = writer.get('review_head')
    if old_head == head and writer.get('review_auto_status') in ('accepted', 'changes', 'exhausted', 'voided'):
        return
    fix_delta = bool(old_head and old_head != head)
    voided = []
    for previous in service.ps()['runs']:
        if (previous.get('item_key') != writer['item_key'] or previous.get('role') != 'implement'
                or not previous.get('review_head')
                or (previous['run_id'] == writer['run_id'] and not fix_delta)):
            continue
        previous['review_auto_status'] = 'voided'
        SpawnService._save(service, previous)
        voided.extend(previous.get('review_runs', []))
        for run_id in previous.get('review_runs', []):
            run = service.result(run_id)
            if run.get('ended') is None:
                cancelled = await service.cancel(run_id)
                if cancelled.get('survivors'):
                    raise ValueError('old review kill incomplete')
    repo = writer['cwd']
    checks = await asyncio.to_thread(CIAdapter().check_runs, repo, head)
    # The remote exact-SHA lookup also proves the head was pushed. No exit-0 inference.
    if not checks or _ci_conclusion(checks, head) is not None:
        raise ValueError('pushed head has no green checks')
    item = await asyncio.to_thread(keeper.item, writer['item_key'])
    if item.get('key') != writer['item_key'] or item.get('review_tier') not in ('standard', 'high'):
        raise ValueError('item identity or review tier unavailable')
    writers = sorted(writer_parts([*writer.get('given_writer_families', []), writer.get('writer_family')]))
    if not writers:
        raise ValueError('writer family unavailable')
    decisions = await asyncio.to_thread(keeper.decisions, item['linked_decisions'])
    card = dict(estimate_tokens=20000, item_key=item['key'],
                review_tier=item['review_tier'], writer_family=writers,
                done_criteria=item['done_criteria'], decisions=decisions,
                diff_ref=dict(repo=repo, base=old_head or (writer.get('diff_ref') or {}).get('base', 'origin/main'), head=head))
    plan = service._route(card, 'review', writers)
    if not plan.legs:
        raise ValueError('review route unavailable')
    writer = service.result(writer['run_id'])
    if changed.is_set() or writer['completion']['head_sha'] != head:
        return
    need = 1 if fix_delta else (2 if item['review_tier'] == 'high' else 1)
    writer.update(review_head=head, review_runs=[], review_findings=[], review_auto_status='running',
                  review_checks=dict(head_sha=head, green=True))
    writer['review_voided'] = voided
    SpawnService._save(service, writer)
    families = set()
    prompt = ('Review this card against its current done criteria and decisions. '
              'Report findings, TEST_GAMING: no|yes|unsure and a final VERDICT: ACCEPT or CHANGES.\n'
              + json.dumps(card, sort_keys=True))
    for leg in plan.legs:
        if changed.is_set() or service.result(writer['run_id']).get('review_auto_status') == 'voided':
            return
        family = seat_family(leg.family)
        if family in writers or family in families:
            continue
        if not (Path(os.environ['PG_REVIEWER_TOKEN_DIR']) / f'reviewer-{family}.token').is_file():
            raise ValueError('review family token unavailable')
        run = await service.spawn(card=card, prompt=prompt, role='review', writer_families=writers,
            _review_writer=writer['run_id'],
            _plan=replace(plan, legs=(leg,), review_seats=(leg,), error=None), no_fallthrough=True)
        writer = service.result(writer['run_id'])
        writer['review_runs'].append(run['run_id'])
        SpawnService._save(service, writer)
        if changed.is_set() or writer.get('review_auto_status') == 'voided':
            return
        task = service.tasks.get(run['run_id'])
        if task:
            wake = asyncio.create_task(changed.wait())
            try:
                await asyncio.wait((task, wake), return_when=asyncio.FIRST_COMPLETED)
            finally:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if changed.is_set():
                return
        run = service.result(run['run_id'])
        if service.result(writer['run_id']).get('review_auto_status') == 'voided':
            return
        try:
            evidence, text = seat.evidence_from_record(service._path(run['run_id']).parent)
            ok, index = seat.parse_verdict(text)
        except ValueError:
            continue
        served_family = seat.model_family(evidence.get('served_model'))
        if not served_family or served_family in writers:
            continue
        if ok is False:
            writer.update(review_findings=[seat.findings(text, index)], review_auto_status='changes')
            break
        journal = service._path(run['run_id']).with_name('verdict.json')
        if ok is True and journal.exists() and json.loads(journal.read_text()).get('status') == 'delivered':
            families.add(served_family)
        if len(families) >= need:
            writer['review_auto_status'] = 'accepted'
            break
    else:
        writer['review_auto_status'] = 'exhausted'
    SpawnService._save(service, writer)
