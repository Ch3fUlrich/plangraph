"""Spawn identity annotation and prelaunch seat admission."""
import os
import re
import subprocess

from plangraph.routing_errors import SpawnRefused


def valid_item(value):
    return (isinstance(value, str) and len(value) <= 200
            and re.fullmatch(r'[A-Za-z0-9._-]+:[A-Za-z0-9._:-]{1,180}', value) is not None)


def annotate_item(record, card, *, inherit=False):
    value = card.get('item_key') if isinstance(card, dict) else None
    if value is None or value == '':
        value = card.get('item') if isinstance(card, dict) else None
    if (value is None or value == '') and not inherit:
        value = os.environ.get('PLANGRAPH_SPAWN_ITEM') or None
    if value is not None and not valid_item(value):
        record.setdefault('warnings', []).append('invalid item_key; stored as null')
        value = None
    record['item_key'] = value
    if value is None and record.get('role') in ('review', 'judge'):
        event = dict(event='seat_item_missing', level='warning')
        if event not in record.setdefault('events', []):
            record['events'].append(event)


def _git(ref, *args, message=None):
    try:
        result = subprocess.run(['git', '-C', ref['repo'], *args],
            **({'stdin': subprocess.DEVNULL} if message is None else {'input': message}),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise SpawnRefused('diff_ref git lookup failed') from None
    if result.returncode:
        raise SpawnRefused('diff_ref git lookup failed')
    return result.stdout.strip()


def admit_seat(card, role):
    """Refuse unpostable seats before admission can queue, forward or launch them."""
    if role not in ('review', 'judge'):
        return card
    identity = {'role': role}
    annotate_item(identity, card)
    value = identity['item_key']
    ref = card.get('diff_ref') if isinstance(card, dict) else None
    if ref is not None:
        from plangraph.planner.seats import _check_diff_ref
        _check_diff_ref(ref)
        head = _git(ref, 'rev-parse', '--verify', ref['head'] + '^{commit}')
        base = _git(ref, 'rev-parse', '--verify', ref['base'] + '^{commit}')
        if base == head:
            raise SpawnRefused('empty_diff: base equals head')
        if value is None:
            message = _git(ref, 'log', '-1', '--format=%B', head)
            trailers = _git(ref, 'interpret-trailers', '--parse', message=message)
            keys = {line.partition(':')[2].strip() for line in trailers.splitlines()
                    if line.partition(':')[0].lower() == 'plan-item'}
            if len(keys) == 1 and valid_item(next(iter(keys))):
                value = keys.pop()
                card = dict(card, item_key=value)
    if value is None:
        raise SpawnRefused('seat_item_missing: card needs item_key (or item)')
    return card
