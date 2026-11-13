"""Pre-route omniroute quota gate (SPAWNER-QUOTA-SNAPSHOT-FAILFAST).

Rows are omniroute ``quota_snapshots`` (provider, connection_id, window_key,
remaining_percentage, is_exhausted, next_reset_at, created_at; camelCase keys
also read). A leg whose provider pool has every account at/below the threshold
reads ``limited`` until the earliest account reset and routes again after it.
A missing, unreadable or malformed source never blocks routing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .clients import effective_gateway_base_url
from .host_keys import _gateway_key
from .liveness import unavailable
from .omniroute_state import _default_fetch

MIN_PCT_ENV = 'PLANGRAPH_QUOTA_MIN_REMAINING_PCT'
#: omniroute model aliases (cx-gpt..., gc/grok...) to the provider whose pool serves them.
ALIASES = {'cx': 'codex', 'cc': 'claude', 'gc': 'grok-cli', 'gh': 'github'}
#: The management route may omit resets; prefer QUOTA_SNAPSHOT_DB when available.
URL_PATH = '/api/usage/utilization?range=7d&aggregateBy=connection'
HOLD_S = 1800.0
CACHE_S = 60.0


def pool_of(model):
    """Provider pool serving a leg model, or None for a leg with no known pool."""
    head, _, rest = str(model or '').partition('/')
    if head == 'agy':
        return 'antigravity'
    alias = re.match(r'(cx|cc|gc|gh)[-/]', rest) if head == 'omniroute' else None
    return ALIASES[alias[1]] if alias else None


def _ts(value):
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()


def _get(row, *names):
    return next((row[n] for n in names if row.get(n) is not None), None)


def pool_down_until(rows, provider, now, *, min_pct=1.0):
    """Epoch until which every account of ``provider`` is at/below ``min_pct`` percent, else None."""
    latest = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        name, account = str(row.get('provider') or ''), _get(row, 'connection_id', 'connectionId')
        if account is None and ':' in name:
            name, account = name.split(':', 1)
        if name != provider:
            continue
        seen = _ts(_get(row, 'created_at', 'createdAt', 'timestamp')) or 0.0
        key = (account, _get(row, 'window_key', 'windowKey'))
        if key not in latest or seen >= latest[key][0]:
            latest[key] = (seen, row)
    accounts = {}   # account -> 0.0 while it can serve, else the reset it waits for
    for (account, _), (seen, row) in latest.items():
        accounts.setdefault(account, 0.0)
        try:
            pct = float(_get(row, 'remaining_percentage', 'remainingPercentage', 'remainingPct'))
        except (TypeError, ValueError):
            pct = None
        if _get(row, 'is_exhausted', 'isExhausted') in (1, True, '1', 'true') or (pct is not None and pct <= min_pct):
            reset = _ts(_get(row, 'next_reset_at', 'nextResetAt', 'resetAt'))
            reset = seen + HOLD_S if reset is None else reset
            if reset > now:
                accounts[account] = max(accounts[account], reset)
    return None if not accounts or 0.0 in accounts.values() else min(accounts.values())


def fetch_snapshot_rows(env=None, *, fetch=None):
    """Read-only snapshot DB or management API; LookupError when unreadable."""
    source = os.environ if env is None else env
    if source.get('QUOTA_SNAPSHOT_DB'):
        try:
            db = sqlite3.connect(Path(source['QUOTA_SNAPSHOT_DB']).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                cursor = db.execute('SELECT provider, connection_id, window_key, remaining_percentage, '
                                    'is_exhausted, next_reset_at, created_at FROM quota_snapshots')
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row)) for row in cursor]
            finally:
                db.close()
        except Exception:
            raise LookupError('quota snapshots unreadable') from None
    base = (effective_gateway_base_url(source) or '').rstrip('/')
    base = base[:-3] if base.endswith('/v1') else base
    try:
        key = _gateway_key(source)
    except (Exception, SystemExit):
        key = None
    if not base or not key:
        raise LookupError('gateway not configured')
    try:
        status, body = (fetch or _default_fetch)(base + URL_PATH, {'Authorization': 'Bearer ' + key}, 5.0)
        data = json.loads(body)
        rows = data.get('data') if isinstance(data, dict) else data
    except Exception:
        raise LookupError('quota snapshots unreadable') from None
    if status != 200 or not isinstance(rows, list):
        raise LookupError('quota snapshots unreadable')
    observed = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
    return [dict(row, created_at=observed) if isinstance(row, dict) and
            _get(row, 'created_at', 'createdAt', 'timestamp') is None else row for row in rows]


def _min_pct():
    try:
        return float(os.environ.get(MIN_PCT_ENV, 1.0))
    except ValueError:
        return 1.0


class QuotaGate:
    """Turns a leg-status read into ``limited`` while the leg's omniroute pool is exhausted."""

    def __init__(self, rows=None, *, clock=time.time, ttl_s=CACHE_S):
        self.rows, self.clock, self.ttl_s = rows, clock, ttl_s
        self._at, self._cached = None, []

    def _snapshot(self):
        if self._at is None or self.clock() - self._at >= self.ttl_s:
            try:
                self._cached = (self.rows or fetch_snapshot_rows)()
            except Exception:
                self._cached = []   # missing or unreadable: routing proceeds as without the gate
            self._at = self.clock()
        return self._cached

    def apply(self, model, status, now=None):
        provider = pool_of(model)
        if provider is None or unavailable(status):
            return status
        now = self.clock() if now is None else now
        until = pool_down_until(self._snapshot(), provider, now, min_pct=_min_pct())
        if until is None:
            return status
        return dict(state='limited', reason='quota_snapshot', evidence=dict(
            status=429, body_excerpt=f'omniroute {provider} pool at 0%, resets at {until:.0f}',
            reset_at=until, ts=now, caller='quota-snapshot'))
