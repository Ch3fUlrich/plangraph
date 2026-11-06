"""Spawn-time item identity; invalid optional metadata never refuses a run."""
import os
import re


def valid_item(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', value) is not None


def annotate_item(record, card, *, inherit=False):
    value = card.get('item') if isinstance(card, dict) else None
    if (not isinstance(value, str) or not value) and not inherit:
        value = os.environ.get('PLANGRAPH_SPAWN_ITEM') or None
    if value is not None and not valid_item(value):
        record.setdefault('warnings', []).append('invalid item_key; stored as null')
        value = None
    record['item_key'] = value
