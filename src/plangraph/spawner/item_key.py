"""Spawn-time item identity; invalid optional metadata never refuses a run."""
import os
import re


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
    from .round_cap import round_ref, work_refs
    if refs := work_refs(card):
        record['round_refs'] = refs
    if (ref := round_ref(card)) is not None:
        record['round_ref'] = ref
    if value is None and record.get('role') in ('review', 'judge'):
        event = dict(event='seat_item_missing', level='warning')
        if event not in record.setdefault('events', []):
            record['events'].append(event)
