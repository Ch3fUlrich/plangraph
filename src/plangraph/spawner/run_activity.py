"""Harness-event liveness for the run watchdog (D-3605).

Any harness event -- a tool call starting or ending (reads, greps and globs too), assistant text or
streamed tokens, a reasoning step -- proves a run is working.  The stall rules read events as well as
edits, so "no edit yet" never stops a run that is reading; a writer that shows no event at all within
``first_event_s`` of launch is hung (``hang_no_first_event``).  ``status`` and ``ps`` print the facts.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time

from .opencode_isolation import WRITER_ROLES

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
HEADER = re.compile(r'^>\s+\S+\s+·\s+\S+')
FIRST_EVENT_S = 90.0       # a writer silent this long after launch is hung (card first_event_s, env PLANGRAPH_SPAWN_FIRST_EVENT_S)
HANG_DOWN_S = 900.0        # a hung leg stays booked down this long (env PLANGRAPH_LEG_HANG_DOWN_S)
HEARTBEAT_S = 10.0         # a running record is refreshed this often while events flow
STREAMING = ('opencode',)  # harnesses that stream events; agy and qodercli -p may print only at exit
_TOOL = re.compile(r'tool|command|exec|patch|file_change|bash|mcp|function_call', re.I)
_TOOL_LINE = re.compile(r'\s*(?:[$→←✱⚙]|\|\s+(?:Read|Write|Edit|Bash|Glob|Grep|List|Patch|Task|Todo)\b|tool\b)', re.I)


def is_activity(line):
    line = ANSI.sub('', line).strip()
    return bool(line and not HEADER.match(line))


def kind(line):
    """'tool', 'reasoning' or 'text' for one harness output line; None for a blank line or the banner.

    opencode ``--format json`` events are read by their type (``tool_use``, ``text``, ``reasoning``);
    plain output by the tool-call glyphs of ``opencode run`` ("$ git status", "-> Read file").
    """
    text = ANSI.sub('', line).strip()
    if not text or HEADER.match(text):
        return None
    event = None
    if text.startswith('{') and len(text) <= 16384:
        try:
            event = json.loads(text)
        except ValueError:
            pass
    if isinstance(event, dict):
        names = [str(event.get('type') or event.get('event') or '')]
        for key in ('part', 'item', 'message', 'content_block', 'delta'):
            sub = event.get(key)
            if isinstance(sub, dict):
                names += [str(sub.get('type') or ''), str(sub.get('tool') or '')]
        blob = ' '.join(names)
        return 'reasoning' if re.search(r'reason|think', blob, re.I) else 'tool' if _TOOL.search(blob) else 'text'
    return 'tool' if _TOOL_LINE.match(text) else 'text'


def note(job, complete, partial=''):
    """Count the harness events in newly completed lines, else in a streamed partial line (tokens)."""
    kinds = [k for k in map(kind, complete) if k] or (['text'] if kind(partial) else [])
    if kinds:
        a, now = job.activity, time.time()
        a['events'] = a.get('events', 0) + len(kinds)
        a['tool_events'] = a.get('tool_events', 0) + kinds.count('tool')
        a.setdefault('first_event_ts', now)
        a['last_event_ts'] = now
    return bool(kinds)


def first_event(card):
    """(seconds or None, from_card): card ``first_event_s`` over env PLANGRAPH_SPAWN_FIRST_EVENT_S (0 = off) over 90."""
    from plangraph.planner.routing import SpawnRefused
    if 'first_event_s' in card:
        value = card['first_event_s']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise SpawnRefused('invalid watchdog threshold: first_event_s')
        return float(value), True
    raw = os.environ.get('PLANGRAPH_SPAWN_FIRST_EVENT_S')
    try:
        value = float(raw) if raw else FIRST_EVENT_S
        if not math.isfinite(value) or value < 0:
            raise ValueError
    except ValueError:
        raise SpawnRefused('invalid first event timeout: PLANGRAPH_SPAWN_FIRST_EVENT_S') from None
    return value or None, False


def _per_leg():
    """PLANGRAPH_SPAWN_FIRST_EVENT_LEGS="model=seconds,..." (0 exempts the leg); malformed entries are ignored."""
    legs = {}
    for item in (os.environ.get('PLANGRAPH_SPAWN_FIRST_EVENT_LEGS') or '').split(','):
        name, _, value = item.strip().rpartition('=')
        try:
            seconds = float(value)
        except ValueError:
            continue
        if name and math.isfinite(seconds) and seconds >= 0:
            legs[name] = seconds
    return legs


def first_event_limit(job, limits):
    """Seconds this run may stay without any harness event before it counts as hung; None = rule off.

    Writers on a streaming harness (opencode) use ``limits['first_event_s']``.  Review seats and
    buffering harnesses are exempt unless the card sets ``first_event_s`` or the leg is named in
    PLANGRAPH_SPAWN_FIRST_EVENT_LEGS (full model id or its name after the last "/"; e.g. a longer
    allowance for a big-context leg).
    """
    seconds = limits.get('first_event_s')
    if not seconds or job.role not in WRITER_ROLES:
        return None
    legs, model = _per_leg(), job.model_or_combo or ''
    override = legs.get(model, legs.get(model.rsplit('/', 1)[-1]))
    if override is not None:
        return override or None
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


async def heartbeat(job, save):
    """Save the running record every HEARTBEAT_S while events flow, so status shows a fresh "last event N s ago"."""
    seen = None
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if job.activity.get('events', 0) != seen:
            seen = job.activity.get('events', 0)
            save()


def _span(seconds):
    return '%d s' % seconds if seconds < 120 else '%d min' % (seconds // 60)


def activity_line(record, now=None):
    """The activity facts of one run record as a line, e.g. "working: 37 tool events, last 12 s ago, 0 edits"."""
    now = time.time() if now is None else now
    a = record.get('activity') or {}
    last = a.get('last_event_ts') or record.get('last_activity_ts')
    live = record.get('ended') is None and record.get('status') == 'running'
    events, edits = a.get('events', 0), a.get('edits', 0)
    count = '%d tool events' % a['tool_events'] if a.get('tool_events') else '%d events' % events
    if last is None:
        if not live:
            return 'ended: no events, %d edits' % edits if a else None
        hint = ' (hung at %g s)' % a['first_event_s'] if a.get('first_event_s') else ''
        return 'no events yet: %s since launch%s, %d edits' % (
            _span(max(0, now - (a.get('since') or record.get('started') or now))), hint, edits)
    age = max(0, (now if live else record.get('ended') or now) - last)
    if not live:
        return 'ended: %s, last %s before the end, %d edits' % (count, _span(age), edits)
    return '%s: %s, last %s ago, %d edits' % (
        'quiet' if age >= (a.get('stall_s') or math.inf) else 'working', count, _span(age), edits)


def with_activity(record, now=None):
    """The record plus its ``activity_line`` (a derived view, never saved)."""
    line = activity_line(record, now) if isinstance(record, dict) else None
    return dict(record, activity_line=line) if line else record
