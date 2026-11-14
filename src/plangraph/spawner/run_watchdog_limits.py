"""Validated per-run watchdog limits and role-specific review caps."""
import math
import os
import time
import json
from pathlib import Path


def class_limits(card, role, defaults):
    """Widen defaults only from an explicitly supplied measured p90 table."""
    from plangraph.routing_errors import SpawnRefused
    path = os.environ.get('PLANGRAPH_SPAWN_P90_TABLE')
    name = card.get('card_class')
    if not path or name is None:
        return defaults
    if not isinstance(name, str) or not name:
        raise SpawnRefused('invalid watchdog p90 table or card class')
    try:
        row = json.loads(Path(path).read_text())[name][role]
        if not isinstance(row, dict) or set(row) - {'stall_min', 'max_min'}:
            raise ValueError
        result = dict(defaults)
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError
            result[key] = max(result[key], value)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        raise SpawnRefused('invalid watchdog p90 table or card class') from None


def leg_limits(record, limits):
    """Capped roles share one wall clock, including prompt preparation."""
    if record.get('role') not in ('review', 'implement', 'fix') or limits['max_s'] is None:
        return limits
    deadline = record['started'] + limits['max_s']
    remaining = deadline - time.time()
    if remaining > 0:
        return dict(limits, deadline=deadline, hard_max=True)
    record.update(status='timed_out' if record.get('role') == 'review' else 'timeout', exit_code=124, reason='timeout')
    record.setdefault('events', []).append(dict(event='run_timeout'))
    return None


def thresholds(card=None, *, role=None):
    """Implement defaults are 25/90 minutes; reviews have a 25-minute hard wall.

    Reviews never wait for a writer's first edit, regardless of the cap source.
    """
    from plangraph.planner.routing import SpawnRefused
    card = card or {}
    defaults = dict(stall_min=25 if role in ('implement', 'fix') else 20,
                    first_edit_min=15, max_min=25 if role == 'review' else 90)
    defaults = class_limits(card, role, defaults)
    defaults['stuck_min'] = defaults['stall_min'] + 5
    result = dict(hard_max=role in ('implement', 'fix', 'review'))
    if not isinstance(card.get('hard_first_edit', False), bool): raise SpawnRefused('invalid watchdog threshold: hard_first_edit')
    if role != 'review' and card.get('hard_first_edit'): result['hard_first_edit'] = True
    for key, default in defaults.items():
        if key == 'stuck_min': default = result['stall_s'] / 60 + 5
        value = card.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise SpawnRefused('invalid watchdog threshold: ' + key)
        result[key.replace('_min', '_s')] = value * 60
    timeout = os.environ.get('PLANGRAPH_SPAWN_RUN_TIMEOUT')
    if timeout and 'max_min' not in card:
        try:
            value = float(timeout)
            if not math.isfinite(value) or value <= 0:
                raise ValueError
        except ValueError:
            # M17: a bad env knob is refused before launch (exit 2), like a bad card.
            raise SpawnRefused('invalid run timeout: PLANGRAPH_SPAWN_RUN_TIMEOUT') from None
        result['max_s'] = value
        # An operator-set run timeout is a wall clock: the first-edit hold never extends it (M5).
        result['hard_max'] = True
    interval = os.environ.get('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL')
    try:
        result['interval'] = float(interval) if interval else min(120, result['stall_s'] / 4)
    except ValueError:
        raise SpawnRefused('invalid watchdog interval') from None
    if not math.isfinite(result['interval']) or result['interval'] <= 0:
        raise SpawnRefused('invalid watchdog interval')
    return result
