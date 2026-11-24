"""Spawn-time item identity; invalid optional metadata never refuses a run.

It also holds the Python entry to the duplicate-assignment guard (``pg-spawnd lane-check``):
the roster, the override rule and the admission lock live in Rust, and ``lane_check`` only
hands a submission to that one implementation.
"""
import json
import logging
import os
import re
import subprocess
from pathlib import Path


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


def batch_members(card):
    """The valid item keys of a batch card (`item_keys`), at most MAX_BATCH_KEYS, in order."""
    keys = card.get('item_keys') if isinstance(card, dict) else None
    if not isinstance(keys, list) or len(keys) > MAX_BATCH_KEYS:
        return []
    if not all(valid_item(key) for key in keys):
        return []
    return list(dict.fromkeys(keys))


MAX_BATCH_KEYS = 50


def annotate_item(record, card, *, inherit=False):
    value = card.get('item_key') if isinstance(card, dict) else None
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
    members = batch_members(card)
    if members:
        record['item_keys'] = members
    if record.get('role') == 'review' and (writer_run := native_writer_review(card)):
        record['native_writer_run'] = writer_run
    if value and record.get('role') == 'review' and not inherit:
        request = record.setdefault('request', {})
        request.update(item_key=value, role='review')
        target = card.get('diff_ref') if isinstance(card, dict) else None
        if isinstance(target, dict) and isinstance(target.get('head'), str):
            request.setdefault('review_head', target['head'])
    if value is None and record.get('role') in ('review', 'judge'):
        event = dict(event='seat_item_missing', level='warning')
        if event not in record.setdefault('events', []):
            record['events'].append(event)


#: What the Rust guard prints when it refuses; any other failure is not a refusal.
_REFUSALS = ("lane_conflict:", "lane_override requires")


class LaneRefused(ValueError):
    """An item is held by another live lane, or an override came without a reason."""


def lane_state() -> Path:
    return Path(os.environ.get("PLANGRAPH_SPAWN_STATE") or
                Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "plangraph")


def lane_check(payload: dict, binary: str | None = None) -> None:
    """Refuse (``LaneRefused``) a submission another live lane holds.

    ``payload`` is the ``lane-check`` input (``state``, ``prompt``, ``card``, ``keys``,
    ``reason``, ``reserve`` ...). A host without the installed binary, or one too old to know
    the verb, cannot answer: the Rust launch and submit paths still guard, so this only logs.
    """
    if binary is None:
        from plangraph.fleet_snapshot import _projection_binary
        try:
            binary = _projection_binary()
        except RuntimeError:
            logging.warning("lane guard skipped: pg-spawnd is not installed")
            return
    payload = {"state": str(lane_state()), **payload}
    try:
        result = subprocess.run([binary, "lane-check"], input=json.dumps(payload), text=True,
                                capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("lane guard skipped: %s", exc)
        return
    if result.returncode == 0:
        return
    message = (result.stderr or result.stdout).strip()
    if message.startswith(_REFUSALS):
        raise LaneRefused(message)
    logging.warning("lane guard skipped: %s", message[:200])
