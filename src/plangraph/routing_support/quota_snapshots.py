"""OmniRoute provider-state and quota snapshots for pre-route admission.

Rows are omniroute ``quota_snapshots`` (provider, connection_id, window_key,
remaining_percentage, is_exhausted, next_reset_at, created_at; camelCase keys
also read). A leg whose provider pool has every account at/below the threshold
reads ``limited`` until the earliest account reset and routes again after it.
A missing, unreadable or malformed source never blocks routing.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .clients import effective_gateway_base_url
from .opencode_render import (
    _NoManagementRedirect, gateway_management_base_url, load_token, valid_gateway_endpoint,
)
from .liveness import unavailable

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
        return None  # D-5966: CLI quotas belong to individual models, not the gateway pool.
    alias = re.match(r'(cx|cc|gc|gh)[-/]', rest) if head == 'omniroute' else None
    return ALIASES[alias[1]] if alias else None


def _ts(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
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


_SNAPSHOT_COLUMNS = ('provider, connection_id, window_key, remaining_percentage, '
                     'is_exhausted, next_reset_at, created_at')


def _plain(value):
    """A cell JSON can carry: a BLOB or a non-finite number is one bad value, not a bad reply."""
    if isinstance(value, bytes) or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return value


_TIME_COLUMNS = ('next_reset_at', 'created_at')


def _decimal(value):
    """A numeric time as its shortest decimal text: a JSON number is rounded by readers that
    parse it as a float, the text keeps the digits the store holds."""
    if isinstance(value, float):
        return repr(value)
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _history(db):
    """Every stored row for the pool readout, which tolerates a malformed row: undecodable text
    is replaced and an unsupported cell becomes None, so independent healthy rows stay readable."""
    db.text_factory = lambda raw: raw.decode('utf-8', 'replace')
    cursor = db.execute('SELECT ' + _SNAPSHOT_COLUMNS + ' FROM quota_snapshots ORDER BY id')
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, map(_plain, row))) for row in cursor]
    for row in rows:
        for column in _TIME_COLUMNS:
            row[column] = _decimal(row[column])
    return rows


def fetch_snapshot_rows(env=None, *, fetch=None, latest='id'):
    """Read-only snapshot DB or management API; LookupError when unreadable.

    ``latest='id'`` (routing) keeps the highest id per provider, connection and window and reads
    a malformed row as an unreadable source; ``latest='all'`` (pool readout) returns every
    stored row, oldest id first, for a reader that picks the newest observation itself.
    """
    source = os.environ if env is None else env
    if source.get('QUOTA_SNAPSHOT_DB'):
        try:
            db = sqlite3.connect(Path(source['QUOTA_SNAPSHOT_DB']).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                if latest == 'all':
                    return _history(db)
                cursor = db.execute('SELECT ' + _SNAPSHOT_COLUMNS + ' FROM quota_snapshots WHERE id IN '
                                    '(SELECT MAX(id) FROM quota_snapshots GROUP BY provider, connection_id, window_key) ORDER BY id')
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row)) for row in cursor]
            finally:
                db.close()
        except Exception:
            raise LookupError('quota snapshots unreadable') from None
    endpoint = effective_gateway_base_url(source) or ''
    if not valid_gateway_endpoint(endpoint):
        raise LookupError('gateway not configured')
    base = gateway_management_base_url(endpoint)
    try:
        key = load_token(source)
    except (Exception, SystemExit):
        key = None
    if not base or not isinstance(key, str) or not key.strip():
        raise LookupError('gateway not configured')
    try:
        status, body = (fetch or _default_fetch)(base + URL_PATH, {'Authorization': 'Bearer ' + key}, 5.0)
        data = json.loads(body)
        rows = data.get('data') if isinstance(data, dict) else data
    except Exception:
        raise LookupError('quota snapshots unreadable') from None
    if status != 200 or not isinstance(rows, list):
        raise LookupError('quota snapshots unreadable')
    return rows


def _min_pct():
    try:
        return float(os.environ.get(MIN_PCT_ENV, 1.0))
    except ValueError:
        return 1.0


def skip_reason(status):
    """The same reset-bearing reason in route and launch read-back."""
    evidence = status.get('evidence') or {}
    reset = evidence.get('reset_at')
    if status['state'] == 'limited' and evidence.get('caller') == 'quota-snapshot' and reset:
        return 'quota_until ' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(reset))
    return None


class QuotaGate:
    """Turns a leg-status read into ``limited`` while the leg's omniroute pool is exhausted."""

    def __init__(self, rows=None, *, clock=time.time, ttl_s=CACHE_S):
        self.rows, self.clock, self.ttl_s = rows, clock, ttl_s
        self._at, self._cached, self._observed = None, [], {}

    def _snapshot(self):
        if self._at is None or self.clock() - self._at >= self.ttl_s:
            try:
                rows = (self.rows or fetch_snapshot_rows)()
                self._cached = []
                for row in rows if isinstance(rows, list) else ():
                    if not isinstance(row, dict):
                        continue
                    key = (row.get('provider'), _get(row, 'connection_id', 'connectionId'), _get(row, 'window_key', 'windowKey'))
                    try:
                        down = float(_get(row, 'remaining_percentage', 'remainingPercentage', 'remainingPct')) <= _min_pct()
                    except (TypeError, ValueError):
                        down = False
                    down |= _get(row, 'is_exhausted', 'isExhausted') in (1, True, '1', 'true')
                    if not down or _get(row, 'next_reset_at', 'nextResetAt', 'resetAt') is not None:
                        self._observed.pop(key, None)
                    elif _get(row, 'created_at', 'createdAt', 'timestamp') is None:
                        first = self._observed.setdefault(key, self.clock())
                        row = dict(row, created_at=datetime.fromtimestamp(first, timezone.utc).isoformat())
                    self._cached.append(row)
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


TTL_ENV_VAR = "PLANGRAPH_OMNIROUTE_STATE_TTL_S"


class OmniRouteUnreachable(RuntimeError):
    """State cannot be established; message is a controlled code only."""


class OmniRouteAuthRefused(RuntimeError):
    """Gateway answered but refused the snapshot caller's credentials."""

    def __init__(self, status, gateway_url, principal):
        self.evidence = dict(kind='auth_refused', http_status=status,
                             gateway_url=gateway_url, principal=principal)
        super().__init__(f'auth_refused: HTTP {status} gateway={gateway_url} principal={principal}')


@dataclass(frozen=True)
class ProviderState:
    provider: str
    available: bool
    reason: str
    until: str
    stale: bool = False


def _default_fetch(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoManagementRedirect())
    with opener.open(request, timeout=timeout) as response:
        return response.status, response.read()


def _build(body, now):
    data = json.loads(body)
    rows = data.get("connections") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ValueError
    states = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError
        provider, active = row.get("provider"), row.get("isActive")
        status = row.get("testStatus", "ok")
        if (not isinstance(provider, str) or not provider.strip()
                or type(active) is not bool
                or (status is not None and not isinstance(status, str))):
            raise ValueError
        until = row.get("rateLimitedUntil") or ""
        limited = False
        if until:
            stamp = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
            limited = stamp > now
            until = stamp.isoformat()
        available = active and status in ("ok", "success", "active") and not row.get("errorCode")
        reason = "" if available else "inactive"
        if limited:
            available, reason = False, "rate_limited"
        state = ProviderState(provider, available, reason, until if not available else "")
        if provider not in states or available:
            states[provider] = state
    return states


class OmniRouteState:
    """Serialized refresh with injected transport and monotonic cache clock."""

    def __init__(self, base_url, key, *, principal='unknown', ttl_s=60, fetch=None,
                 clock=time.monotonic, wall_clock=lambda: datetime.now(timezone.utc)):
        try:
            ttl_s = float(ttl_s)
        except (TypeError, ValueError):
            raise ValueError("invalid_ttl") from None
        if not math.isfinite(ttl_s) or ttl_s <= 0:
            raise ValueError("invalid_ttl")
        self.base_url = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""
        self._key = key if isinstance(key, str) and key.strip() else None
        self._principal = principal
        self._ttl_s, self._fetch = ttl_s, fetch or _default_fetch
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = threading.Lock()
        self._good, self._good_at = None, None

    def _read(self):
        if not self.base_url:
            raise OmniRouteUnreachable("no_endpoint")
        if not self._key:
            raise OmniRouteUnreachable("no_key")
        try:
            status, body = self._fetch(self.base_url + "/api/providers",
                                       {"Authorization": "Bearer " + self._key}, 8.0)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status not in (401, 403):
                raise OmniRouteUnreachable("transport_error") from None
        except Exception:
            raise OmniRouteUnreachable("transport_error") from None
        if status in (401, 403):
            from .leg_state import _redact
            gateway = self.base_url if valid_gateway_endpoint(self.base_url) else 'unknown'
            principal = _redact(str(self._principal).replace(self._key, '[REDACTED]'), 80)
            raise OmniRouteAuthRefused(status, gateway.replace(self._key, '[REDACTED]'),
                                       principal) from None
        if status != 200:
            raise OmniRouteUnreachable("http_error")
        try:
            return _build(body, self._wall_clock())
        except Exception:
            raise OmniRouteUnreachable("bad_snapshot") from None

    def snapshot(self):
        """Return defensive snapshots; stale is explicit and bounded."""
        with self._lock:
            now = self._clock()
            if self._good is not None and now - self._good_at < self._ttl_s:
                return dict(self._good)
            try:
                states = self._read()
            except OmniRouteAuthRefused:
                self._good = None
                raise
            except OmniRouteUnreachable:
                if self._good is not None and self._clock() - self._good_at <= 2 * self._ttl_s:
                    return {name: replace(state, stale=True)
                            for name, state in self._good.items()}
                self._good = None
                raise
            self._good, self._good_at = states, self._clock()
            return dict(states)

    def provider_state(self, provider):
        return self.snapshot().get(provider)


def from_env(env=None, *, key_resolver=None, **kwargs):
    """Validate the configured endpoint before resolving a key."""
    source = os.environ if env is None else env
    endpoint = effective_gateway_base_url(source) or ""
    base = gateway_management_base_url(endpoint) if valid_gateway_endpoint(endpoint) else ""
    key = None
    if base:
        try:
            key = (key_resolver or load_token)(source)
        except (Exception, SystemExit):
            pass
    kwargs.setdefault("ttl_s", source.get(TTL_ENV_VAR, 60))
    kwargs.setdefault("principal", source.get('PLANGRAPH_PRINCIPAL') or 'unknown')
    return OmniRouteState(base, key, **kwargs)
