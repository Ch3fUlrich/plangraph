"""Spawn-time item identity; invalid optional metadata never refuses a run."""
import os
import re


def valid_item(value):
    return (isinstance(value, str) and len(value) <= 200
            and re.fullmatch(r'[A-Za-z0-9._-]+:[A-Za-z0-9._:-]{1,180}', value) is not None)


def annotate_item(record, card, *, inherit=False, review_writer=None):
    if isinstance(card, dict) and card.get('diff_ref'):
        record['diff_ref'] = card['diff_ref']
    if review_writer and record.get('role') == 'review':
        record['review_writer'] = review_writer
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
