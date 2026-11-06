"""Exact-leg CLI execution and auditable, bounded seat fall-through."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from plangraph.planner.seats import seat_family
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.spawner.fact_check import parse_report
from tools.plan_loop_ledger import parse_reply
from tools.plan_loop_seats import SelectionRefused, pick


def training_allowed(bundle, material):
    if material == 'public':
        return True
    scrub = bundle.scrub_record
    return (material == 'scrubbed' and scrub.get('hits') == 0
            and bool(scrub.get('words_sha'))
            and scrub.get('bundle_sha') == hashlib.sha256(bundle.text.encode()).hexdigest())


def call_cli(directory, kind, leg, bundle, policy, *, seat_id, spawn=subprocess.run,
             dry_run=False):
    """Prompt bytes stay in files; the injectable callable has subprocess.run's API."""
    directory = Path(directory)
    if not seat_id or Path(seat_id).name != seat_id or seat_id in {'.', '..'}:
        raise ValueError('invalid seat id')
    directory.mkdir(parents=True, exist_ok=True)
    prompt = f'{seat_id}.prompt.md'
    card = f'{seat_id}.card.json'
    (directory / prompt).write_text(bundle.text, encoding='utf-8')
    (directory / card).write_text(json.dumps({
        'estimate_tokens': math.ceil(len(bundle.text) / policy.chars_per_token),
        'max_min': policy.seat_max_min}), encoding='utf-8')
    argv = [sys.executable, '-m', 'plangraph.spawner.cli', 'spawn', '--role',
            'fact-check' if kind == 'r0' else 'review', '--leg', leg.id,
            '--override-reason', 'plan-loop.toml exact leg']
    if kind != 'r0':
        argv += ['--writer-family', policy.planner_family]
    argv += ['--prompt-file', prompt, '--card', '@' + card,
             '--wait', str(policy.seat_max_min * 60), '--json']
    if dry_run:
        argv.append('--dry-run')
    env = dict(os.environ)
    env.pop('PLANGRAPH_CONTEXT_STRICT', None)
    return spawn(argv, cwd=directory, env=env, capture_output=True, text=True,
                 check=False)


def read_record(record, leg, *, kind='confirm', returncode=0):
    """Never use the record-level concatenated stdout or infer a witness."""
    row = {key: record.get(key) for key in
           ('run_id', 'served_model', 'served_family', 'witness_kind', 'request_side_model')}
    family = seat_family(leg.family)
    served = record.get('served_family')
    known = {seat_family(item.family) for item in DEFAULT_LEGS}
    witnessed = (isinstance(served, str) and seat_family(served) in known)
    witnessed |= (record.get('witness_kind') == 'request-side-opencode-store'
                  and record.get('request_side_model') == leg.model)
    row.update(family=family, leg=leg.id, witnessed=bool(witnessed),
               status='failed', reason='', verdict=None, findings=[], attempt_used=True)
    status = record.get('status')
    reason = record.get('reason') or status or 'invalid_record'
    prelaunch = {'limited', 'not_served_on_host', 'harness_missing', 'over_budget'}
    render_refused = any(event.get('event', event.get('kind')) == 'render_refused'
                         and event.get('reason') == 'opencode_tools_not_read_only'
                         for event in record.get('events', []) if isinstance(event, dict))
    if returncode == 2 or status == 'refused' or reason in prelaunch or render_refused:
        row.update(reason=reason, attempt_used=False)
        return row
    if status != 'completed':
        row['reason'] = reason
        return row
    if served and seat_family(served) in known and seat_family(served) != family:
        row['reason'] = 'served_family_mismatch'
        return row
    attempts = record.get('attempts') or []
    text = (attempts[-1].get('stdout_tail') or '') if attempts else ''
    row['reply'] = text
    if not text.strip() or '[... output truncated ...]' in text:
        row['reason'] = 'output_truncated' if text.strip() else 'empty_reply'
    elif kind == 'r0':
        report = parse_report(text)
        row.update(report=report, status='completed' if report.ok else 'failed',
                   reason=report.reason or '')
    else:
        row.update(parse_reply(text))
    return row


def run_seat(kind, state, policy, directory, build_bundle, *, spawn=subprocess.run,
             seat_id='seat'):
    """Fill one seat, recording skips; failures exclude the launched family.

    The round driver supplies a shared remaining failure allowance in policy when
    filling multiple seats. Selection remains pure and caller state is untouched.
    """
    remaining = policy
    rows, failed, excluded = [], 0, set()
    for item in policy.leg:
        if item.training and policy.material == 'private':
            rows.append(dict(leg=item.id, family=seat_family(_leg_family(item.id)),
                             status='skipped', reason='training_leg_private_material',
                             attempt_used=False, witnessed=False))
    remaining = replace(remaining, leg=tuple(item for item in remaining.leg
                        if not (item.training and policy.material == 'private')))
    while remaining.leg:
        try:
            leg = pick(kind, state, remaining)[0]
        except SelectionRefused:
            break
        family = seat_family(leg.family)
        config = next(item for item in remaining.leg if item.id == leg.id)
        bundle = build_bundle(leg)
        reason = bundle.skipped_reason
        if config.training and not training_allowed(bundle, policy.material):
            reason = 'training_leg_private_material'
        if reason:
            row = dict(leg=leg.id, family=family, status='skipped', reason=reason,
                       attempt_used=False, witnessed=False)
        else:
            try:
                result = call_cli(directory, kind, leg, bundle, policy,
                                  seat_id=f'{seat_id}-{len(rows) + 1}', spawn=spawn)
                try:
                    record = json.loads(result.stdout)
                    if not isinstance(record, dict):
                        raise ValueError('invalid record')
                except (ValueError, TypeError):
                    record = {'status': 'failed', 'reason': 'invalid_record'}
                row = read_record(record, leg, kind=kind, returncode=result.returncode)
            except OSError:
                row = dict(leg=leg.id, family=family, status='failed',
                           reason='harness_missing', attempt_used=False, witnessed=False)
        rows.append(row)
        if row['status'] == 'completed':
            if kind == 'r0':
                target = Path(directory) / 'report.md'
                pending = target.with_suffix('.md.tmp')
                pending.write_text(row['reply'], encoding='utf-8')
                os.replace(pending, target)
            return dict(status='completed', failed_attempts=failed, seats=rows)
        if row['attempt_used']:
            failed += 1
            excluded.add(family)
            if failed >= policy.max_failed_attempts:
                break
        remaining = replace(remaining, leg=tuple(
            item for item in remaining.leg if item.id != leg.id and
            seat_family(_leg_family(item.id)) not in excluded))
    return dict(status='stalled', failed_attempts=failed, seats=rows,
                reason=f'BLOCKED plan:{state.slug} | review-loop-stalled | '
                       'options: wait for a family, *waive by D-row')


def _leg_family(id):
    return next(leg.family for leg in DEFAULT_LEGS if leg.id == id)
