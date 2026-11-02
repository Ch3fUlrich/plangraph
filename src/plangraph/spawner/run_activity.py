"""Harness-event liveness (D-3622): any non-blank, non-banner output line proves
the run is alive.  A run with no event within ``first_event_s`` of launch is hung
(``hang_no_first_event``) and its leg is booked down for ``hang_down_s()``."""
import math
import os
import re

from .opencode_isolation import WRITER_ROLES

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
HEADER = re.compile(r'^>\s+\S+\s+·\s+\S+')
FIRST_EVENT_S = 90.0       # a writer silent this long after launch is hung (card first_event_s, env PLANGRAPH_SPAWN_FIRST_EVENT_S)
HANG_DOWN_S = 900.0        # a hung leg stays booked down this long (env PLANGRAPH_LEG_HANG_DOWN_S)
STREAMING = ('opencode',)  # harnesses that stream events; agy and qodercli -p may print only at exit


def is_activity(line):
    line = ANSI.sub('', line).strip()
    return bool(line and not HEADER.match(line))


def first_event(card):
    """(seconds or None, from_card): card ``first_event_s`` (0 = off) over env PLANGRAPH_SPAWN_FIRST_EVENT_S (0 = off) over 90."""
    from plangraph.planner.routing import SpawnRefused
    if 'first_event_s' in card:
        value = card['first_event_s']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise SpawnRefused('invalid watchdog threshold: first_event_s')
        return float(value) or None, True   # 0 turns the rule off, like the env knob
    raw = os.environ.get('PLANGRAPH_SPAWN_FIRST_EVENT_S')
    try:
        value = float(raw) if raw else FIRST_EVENT_S
        if not math.isfinite(value) or value < 0:
            raise ValueError
    except ValueError:
        raise SpawnRefused('invalid first event timeout: PLANGRAPH_SPAWN_FIRST_EVENT_S') from None
    return value or None, False


def first_event_limit(job, limits):
    """Seconds a run may stay without any harness event before it counts hung; None = rule off (review seats and buffering harnesses exempt)."""
    seconds = limits.get('first_event_s')
    if not seconds or job.role not in WRITER_ROLES:
        return None
    harness = job.command[0] if job.command else ''
    return seconds if limits.get('first_event_card') or harness in STREAMING else None


def hang_event(hang_s, elapsed):
    return {'event': 'hang_no_first_event', 'first_event_s': hang_s, 'elapsed_s': round(elapsed, 1),
            'line': 'hang_no_first_event: no harness event within %g s of launch' % hang_s}


def hang_down_s():
    """TTL a hung leg stays booked down; a malformed PLANGRAPH_LEG_HANG_DOWN_S falls back to the default."""
    try:
        value = float(os.environ.get('PLANGRAPH_LEG_HANG_DOWN_S') or HANG_DOWN_S)
    except ValueError:
        return HANG_DOWN_S
    return value if math.isfinite(value) and value >= 0 else HANG_DOWN_S
