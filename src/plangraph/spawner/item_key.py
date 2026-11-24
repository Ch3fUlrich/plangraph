"""Spawn-time item identity; invalid optional metadata never refuses a run."""
import os
import re


def valid_item(value):
    return (isinstance(value, str) and len(value) <= 200
            and re.fullmatch(r'[A-Za-z0-9._-]+:[A-Za-z0-9._:-]{1,180}', value) is not None)


def native_writer_review(card):
    """Return the writer run ID carried by a native workspace review card."""
    if isinstance(card, dict) and card.get('review_workspace') is True:
        run = card.get('native_writer_run')
        if isinstance(run, str) and re.fullmatch(r'run-[A-Za-z0-9]{1,64}', run):
            return run
    return None


def part_keys(card):
    """Every valid item key of the part a card covers (``item_keys``), each once, in card order."""
    keys = card.get('item_keys') if isinstance(card, dict) else None
    return list(dict.fromkeys(k for k in keys if valid_item(k))) if isinstance(keys, list) else []


def annotate_item(record, card, *, inherit=False):
    value = card.get('item_key') if isinstance(card, dict) else None
    keys = part_keys(card)
    if keys and not value:
        value = keys[0]
    if value is None or value == '':
        value = card.get('item') if isinstance(card, dict) else None
    if (value is None or value == '') and not inherit:
        value = os.environ.get('PLANGRAPH_SPAWN_ITEM') or None
    if value is not None and not valid_item(value):
        record.setdefault('warnings', []).append('invalid item_key; stored as null')
        value = None
    if value is not None:
        project, _, item = value.partition(':')
        while item.startswith(project + ':') and item[len(project) + 1:]:
            item = item[len(project) + 1:]
        value = project + ':' + item
    record['item_key'] = value
    if value is not None and keys:
        record['item_keys'] = list(dict.fromkeys([value, *keys]))
    if record.get('role') == 'review' and (writer_run := native_writer_review(card)):
        record['native_writer_run'] = writer_run
    if value and record.get('role') == 'review' and not inherit:
        request = record.setdefault('request', {})
        request.update(item_key=value, role='review')
        if 'item_keys' in record:
            request['item_keys'] = list(record['item_keys'])
        target = card.get('diff_ref') if isinstance(card, dict) else None
        if isinstance(target, dict) and isinstance(target.get('head'), str):
            request.setdefault('review_head', target['head'])
    if value is None and record.get('role') in ('review', 'judge'):
        event = dict(event='seat_item_missing', level='warning')
        if event not in record.setdefault('events', []):
            record['events'].append(event)
