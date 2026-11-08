"""Spawn-time item identity; invalid optional metadata never refuses a run."""
import os; import re; import json; import subprocess; import sys
import threading
from pathlib import Path
def valid_item(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', value) is not None
def annotate_item(record, card, *, inherit=False, prompt=''):
    value = (card.get('item_key') or card.get('item')) if isinstance(card, dict) else None
    if (not isinstance(value, str) or not value) and not inherit:
        value = os.environ.get('PLANGRAPH_SPAWN_ITEM') or None
    if value is None and not inherit:
        matches = set(re.findall(r'\bplangraph:[A-Za-z0-9._:-]+', prompt or record.get('prompt', '')))
        if len(matches) == 1:
            value = matches.pop()
    if value is not None and not valid_item(value):
        record.setdefault('warnings', []).append('invalid item_key; stored as null')
        value = None
    record['item_key'] = value
    if value is None and record.get('role') in ('review', 'judge'):
        event = dict(event='seat_item_missing', level='warning')
        if event not in record.setdefault('events', []):
            record['events'].append(event)
def _post_verdict(args):
    poster = Path(__file__).resolve().parents[3] / 'tools/seat_verdict.py'
    result = subprocess.run([sys.executable, str(poster), *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, timeout=45, check=False)
    if result.returncode not in (0, 2, 3, 4):
        raise RuntimeError('poster crashed or IO failed')
    return result.returncode
def schedule_autopost(record, path):
    """Keep optional network work outside completion persistence and delivery."""
    def work():
        copy = dict(record)
        copy.pop('seat_autopost_attempted', None)
        try:
            if autopost(copy, path):
                from .private_io import atomic_write_private
                atomic_write_private(path.parent / 'seat-autopost.json', json.dumps(copy['events'][-1]))
        except Exception:
            pass  # Diagnostics persistence must not affect completion either.
    if (os.environ.get('PLANGRAPH_SEAT_AUTOPOST') == '1'
            and record.get('status') == 'completed' and record.get('role') in ('review', 'judge')
            and not record.get('seat_autopost_attempted')):
        record['seat_autopost_attempted'] = True
        threading.Thread(target=work, daemon=True).start()
def autopost(record, path):
    """Best-effort, opt-in completion hook; never jeopardize a finished run."""
    if (os.environ.get('PLANGRAPH_SEAT_AUTOPOST') != '1'
            or record.get('role') not in ('review', 'judge')
            or record.get('status') != 'completed' or record.get('seat_autopost_attempted')):
        return False
    record['seat_autopost_attempted'] = True
    event = dict(event='seat_autopost', level='warning')
    try:
        key = record.get('item_key')
        head = record.get('completion', {}).get('head_sha')
        url = os.environ.get('PG_URL')
        if not key or not head or not record.get('served_model') or not url:
            raise ValueError('missing item, completion head, served family, or PG_URL')
        snapshot = Path(os.environ.get('PLANGRAPH_SEAT_SNAPSHOT', 'plan.snapshot.json'))
        data = json.loads(snapshot.read_text()) if snapshot.exists() else {}
        items = data.get('items', [])
        if isinstance(items, dict):
            items = items.values()
        item = next((i for i in items if i.get('key') == key), {'key': key})
        # Only exact-head, verified implement evidence can identify writers.
        writers = sorted({e['family'] for e in item.get('writer_evidence', [])
                          if e.get('verified') is True and e.get('head_sha') == head
                          and e.get('role') == 'implement' and e.get('family')})
        if not writers:
            raise ValueError('unknown writer families')
        code = _post_verdict(['--seat-dir', str(path.parent), '--head', head,
                             '--key', key, '--writer-family', ','.join(writers),
                             '--token-dir', os.environ.get('PLANGRAPH_SEAT_TOKEN_DIR',
                                 str(Path.home() / '.config/plangraph/tokens')),
                             '--url', url])
        event.update(exit_code=code, level='info' if code == 0 else 'warning')
    except Exception as exc:
        event['reason'] = type(exc).__name__
    record.setdefault('events', []).append(event)
    return True
