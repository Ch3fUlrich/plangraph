"""Validated per-run watchdog limits and role-specific review caps."""
import math
import os
import time


def leg_limits(record, limits):
    """Review fallthroughs share one wall clock, including prompt preparation."""
    if record.get('role') != 'review' or limits['max_s'] is None:
        return limits
    deadline = record['started'] + limits['max_s']
    remaining = deadline - time.time()
    if remaining > 0:
        return dict(limits, deadline=deadline, hard_max=True)
    record.update(status='timed_out', exit_code=124, reason='timeout')
    record.setdefault('events', []).append(dict(event='run_timeout'))
    return None


def thresholds(card=None, *, role=None):
    """Unset run timeout gives reviews 25 minutes; empty keeps the 90-minute wall.

    Reviews never wait for a writer's first edit, regardless of the cap source.
    """
    from plangraph.planner.routing import SpawnRefused
    card = card or {}
    result = {'hard_max': False}   # table max_min may carry the first-edit hold (M5)
    if role == 'review':
        result['hard_max'] = True  # reviews never wait for a writer's first edit
    if not isinstance(card.get('hard_first_edit', False), bool): raise SpawnRefused('invalid watchdog threshold: hard_first_edit')
    if role != 'review' and card.get('hard_first_edit'): result['hard_first_edit'] = True
    for key, default in [('stall_min', 20), ('first_edit_min', 15), ('max_min', 90)]:   # REMOVE-AT-R10 (D-3769): first_edit_min was 45
        value = card.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise SpawnRefused('invalid watchdog threshold: ' + key)
        result[key.replace('_min', '_s')] = value * 60
    timeout = os.environ.get('PLANGRAPH_SPAWN_RUN_TIMEOUT')
    if timeout is None and role == 'review':
        timeout = '1500'
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
