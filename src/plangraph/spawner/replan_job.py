"""Automatic replan after the round cap (D-5813): the strongest planning leg reads the item,
the PR diff and the open findings, defines slices through the keeper, the PR is closed
unmerged, and routing gets one line. Every external step is an injectable ReplanIO callable."""
import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from plangraph.keeper.note_fold import DESCRIPTION_CAP

SLICE_MAX_LINES = 300
MAX_SLICES = 12
DIFF_MAX_CHARS = 120_000


class ReplanError(Exception):
    """A replan step failed; the message is the reason routing reads."""


@dataclass
class ReplanIO:
    item: Callable      # key -> keeper item dict
    diff: Callable      # (card, ref) -> unified diff text
    job: Callable       # async (card, prompt, plan) -> spawner run record
    define: Callable    # list of item definitions -> None (raises ReplanError)
    close_pr: Callable  # (card, ref, comment) -> None (raises ReplanError)
    notify: Callable    # (item, run_id, one line) -> None


def _run(args, cwd, run=subprocess.run):
    try:
        proc = run(args, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                   timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReplanError(f'{args[0]} failed: {type(exc).__name__}') from None
    if proc.returncode != 0:
        raise ReplanError(f'{args[0]} failed: ' + ((proc.stderr or '').strip().splitlines() or [f'exit {proc.returncode}'])[0][:200])
    return proc.stdout


def card_cwd(service, card):
    return card.get('cwd') or (card.get('diff_ref') or {}).get('repo') or getattr(service.runner, 'default_cwd', None) or os.getcwd()


def git_diff(cwd, card, ref, run=subprocess.run):
    """Local git diff of the card's diff_ref, else the PR's diff through gh."""
    spec = card.get('diff_ref')
    if spec:
        return _run(['git', '-C', spec['repo'], 'diff', '--no-ext-diff', '--no-color',
                     f"{spec['base']}...{spec['head']}", '--'], cwd, run)
    if ref is None:
        raise ReplanError('diff unreadable: the card names no PR, branch or diff_ref')
    return _run(['gh', 'pr', 'diff', ref], cwd, run)


def close_pr(cwd, ref, comment, run=subprocess.run):
    _run(['gh', 'pr', 'close', ref, '--comment', comment], cwd, run)


def _keeper(call):
    from plangraph.keeper.cli_verbs import MissingKeeperURL, RefusedCli, Unreachable
    try:
        return call()
    except (MissingKeeperURL, RefusedCli, Unreachable) as exc:
        raise ReplanError(f'keeper call failed: {type(exc).__name__}: {str(exc)[:160]}') from None


def default_io(service):
    from .stall_watchdog import deliver_event

    def keeper(method, verb, params=None, body=None):
        from plangraph.keeper.cli_verbs import Session
        return _keeper(lambda: Session().call(method, verb, params, body))

    async def job(card, prompt, plan):
        return await service._spawn(card=card, prompt=prompt, role='research', _plan=plan, wait_seconds=None)

    return ReplanIO(
        item=lambda key: keeper('GET', 'show', {'key': key}),
        diff=lambda card, ref: git_diff(card_cwd(service, card), card, ref),
        job=job,
        define=lambda items: keeper('POST', 'create_items', body={'items': items}),
        close_pr=lambda card, ref, comment: close_pr(card_cwd(service, card), ref, comment),
        notify=lambda item, run_id, line: deliver_event(
            service.state, dict(rule='round-cap-replan', kind='alert', recipient='routing',
                                item_key=item, run_id=run_id, text=line)))


def replan_prompt(item, item_row, ref, diff, findings, rounds):
    if len(diff) > DIFF_MAX_CHARS:
        diff = diff[:DIFF_MAX_CHARS] + f'\n[diff truncated: {DIFF_MAX_CHARS} of {len(diff)} chars]'
    row = item_row if isinstance(item_row, dict) else {}
    text = '\n'.join(f'{k}: {row[k]}' for k in ('key', 'title', 'desc', 'description', 'done_criteria') if row.get(k)) or item
    numbered = '\n'.join(f'{n}. {t}' for n, t in enumerate(findings, 1)) or '(the card carries no findings)'
    return (f'Replan {item}: {rounds} implement rounds did not merge PR {ref}. Do not implement.\n'
            f'Split the remaining work into slices of at most {SLICE_MAX_LINES} changed lines, one writer round each, and '
            'assign every open finding to a slice by its number. Answer with one JSON object after your analysis: '
            '{"slices":[{"title":str,"scope":str,"files":[str],"lines":int,"findings":[int]}]}\n\n'
            f'=== ITEM ===\n{text}\n\n=== OPEN FINDINGS ===\n{numbered}\n\n=== PR DIFF ===\n{diff}\n')


def parse_slices(text, count):
    """The last {"slices": [...]} object of the job output, validated against the finding count."""
    starts = [m.start() for m in re.finditer(r'\{\s*"slices"', text or '')]
    if not starts:
        raise ReplanError('slices_missing: the job output carries no slices object')
    try:
        data, _ = json.JSONDecoder().raw_decode(text[starts[-1]:])
    except ValueError:
        raise ReplanError('slices_invalid: the slices object is not valid JSON') from None
    slices = data.get('slices')
    if not isinstance(slices, list) or not 1 <= len(slices) <= MAX_SLICES:
        raise ReplanError(f'slices_invalid: need 1..{MAX_SLICES} slices')
    covered = set()
    for n, one in enumerate(slices, 1):
        if not isinstance(one, dict):
            raise ReplanError(f'slices_invalid: slice {n} is not an object')
        lines = one.get('lines')
        if type(lines) is not int or not 1 <= lines <= SLICE_MAX_LINES:
            raise ReplanError(f'slice_too_large: slice {n} lines={lines!r}, limit {SLICE_MAX_LINES}')
        for field in ('title', 'scope'):
            if not isinstance(one.get(field), str) or not one[field].strip():
                raise ReplanError(f'slices_invalid: slice {n} needs a {field}')
        numbers = one.get('findings', [])
        if not isinstance(numbers, list) or any(type(i) is not int or not 1 <= i <= count for i in numbers):
            raise ReplanError(f'slices_invalid: slice {n} names a finding outside 1..{count}')
        covered.update(numbers)
    if missing := sorted(set(range(1, count + 1)) - covered):
        raise ReplanError('findings_unassigned: ' + ','.join(map(str, missing)))
    return slices


def slice_items(item, ref, rounds, slices, findings):
    """Keeper definitions: one lane per slice, the findings copied verbatim, the item gated on them."""
    project, ident = item.split(':', 1)
    items = []
    for n, one in enumerate(slices, 1):
        verbatim = '\n'.join(f'- {findings[i - 1]}' for i in sorted(set(one.get('findings', []))))
        files = ', '.join(str(f) for f in one.get('files') or ())
        description = (f'Why: {item} did not merge after {rounds} writer rounds; PR {ref} was closed unmerged (D-5813).\n'
                       f"How: {one['scope'].strip()}\nFiles: {files or '-'}\nFindings (verbatim):\n{verbatim or '- none'}")
        if len(description) > DESCRIPTION_CAP:
            raise ReplanError(f'slice_description_too_long: slice {n} has {len(description)} chars, cap {DESCRIPTION_CAP}')
        items.append(dict(project=project, id=f'{ident}-S{n}', title=f"{ident} slice {n}: {one['title'].strip()}"[:200],
                          description=description, kind='lane', under=item,
                          done_criteria=f'Slice of {item} merged with its findings fixed or refuted by a named test; '
                                        f'at most {SLICE_MAX_LINES} changed lines.'))
    return items


async def execute_replan(payload, plan, io, items_path):
    """Read, plan, define, close; returns the facts for the record. Raises ReplanError."""
    from .private_io import atomic_write_private
    item, ref, findings, card = payload['item'], payload['pr'], payload['findings'], payload['replan_card']
    item_row = await asyncio.to_thread(io.item, item)
    diff = await asyncio.to_thread(io.diff, card, ref)
    job = await io.job(card, replan_prompt(item, item_row, ref, diff, findings, payload['rounds']), plan)
    if job.get('status') != 'completed':
        raise ReplanError(f"job_{job.get('status')}: {job.get('error') or job.get('reason') or ''}"[:200])
    items = slice_items(item, ref, payload['rounds'], parse_slices(job.get('stdout_tail'), len(findings)), findings)
    atomic_write_private(items_path, json.dumps(items, sort_keys=True))
    try:
        await asyncio.to_thread(io.define, items)
    except ReplanError as exc:
        raise ReplanError(f'define_failed (PR left open, definitions in {items_path}): {exc}') from None
    keys = [f"{i['project']}:{i['id']}" for i in items]
    closed = 'no_pr'
    if ref is not None:
        comment = f'Closed unmerged by the round-cap replan (D-5813): slices {", ".join(keys)}.'
        try:
            await asyncio.to_thread(io.close_pr, card, ref, comment)
        except ReplanError as exc:
            raise ReplanError(f'pr_close_failed (slices defined: {",".join(keys)}): {exc}') from None
        closed = 'closed'
    return dict(slices=keys, pr=closed, job_run_id=job.get('run_id'))


async def run_marked(service, payload, plan, io, record, save, items_path):
    """Run the job, record the outcome in the marker, send routing exactly one line."""
    item = payload['item']
    try:
        record.update(execute := await execute_replan(payload, plan, io, items_path), status='done')
        line = (f"DONE {item} {payload['pr'] or '-'} round_cap replan: PR {execute['pr']} unmerged, "
                f"slices {','.join(execute['slices'])}, job {execute['job_run_id']}")
    except ReplanError as exc:
        record.update(status='failed', reason=str(exc))
        line = f'FAIL {item} round_cap replan: {exc}'
    except Exception as exc:  # background task boundary: the outcome must reach the marker and routing
        record.update(status='failed', reason=f'{type(exc).__name__}: {exc}'[:200])
        line = f"FAIL {item} round_cap replan: {record['reason']}"
    record['ended'] = time.time()
    save()
    io.notify(item, record['run_id'], ' '.join(line.split())[:600])
