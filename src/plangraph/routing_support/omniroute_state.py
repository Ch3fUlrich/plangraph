"""Cached OmniRoute provider-state reader (SPAWNER-LEG-STATE-FROM-OMNIROUTE).

Leg availability comes from OmniRoute as the single source (D-3624): this
module reads ``GET {base}/api/providers`` and maps each connection row to one
``ProviderState`` per provider.  Failures are visible: a good snapshot is
cached for ``ttl_s`` and served with ``stale=True`` for at most ``2 * ttl_s``
after it; beyond that ``OmniRouteUnreachable`` raises -- an empty or partial
answer is never returned as success.  The gateway key is only a bearer token:
it never appears in a message, log or exception text.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

from . import clients
from .host_keys import _gateway_key

FETCH_TIMEOUT_S = 8.0
DEFAULT_BASE_URL = "http://127.0.0.1:20128"
TTL_ENV_VAR = "PLANGRAPH_OMNIROUTE_STATE_TTL_S"
_UNAVAILABLE_TESTS = ("error", "failed", "unavailable")


class OmniRouteUnreachable(RuntimeError):
    """OmniRoute provider state cannot be established (fail visible)."""


class _FetchFailure(Exception):
    """Internal failure whose ``why`` is safe for exception text (never a key)."""


@dataclass(frozen=True)
class ProviderState:
    provider: str
    available: bool
    reason: str
    until: str  # raw ``rateLimitedUntil`` value; "" when none
    stale: bool


def _default_fetch(url, headers, timeout):
    """(status, body) via urllib; HTTP errors answer too (``HTTPError.code``)."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as exc:  # an HTTP answer is data, not a failure
        return int(exc.code), exc.read()


def _iso_future(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp > datetime.now(timezone.utc)


def _connection_state(row):
    """(available, reason, until) for one OmniRoute connection row."""
    until = str(row.get("rateLimitedUntil") or "")
    available = (row.get("isActive") is True
                 and row.get("testStatus") not in _UNAVAILABLE_TESTS
                 and not row.get("errorCode"))
    reason = ""
    if _iso_future(until):
        available, reason = False, "rate_limited"
    elif not available:
        reason = next((str(v) for v in (row.get("lastErrorType"), row.get("errorCode"),
                                        row.get("testStatus")) if v), "inactive")
    return available, reason, until


def _build(connections, stale):
    """{provider: ProviderState}; a provider is available if ANY row is."""
    merged = {}
    for row in connections:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        if not isinstance(provider, str) or not provider:
            continue
        available, reason, until = _connection_state(row)
        entry = merged.setdefault(provider, [False, "", ""])
        entry[0] = entry[0] or available
        if not entry[1] and reason:
            entry[1] = reason
        if not entry[2] and until:
            entry[2] = until
    return {provider: ProviderState(provider, available,
                                   "" if available else reason, until, stale)
            for provider, (available, reason, until) in merged.items()}


class OmniRouteState:
    """Cached reader for OmniRoute's ``/api/providers`` state."""

    def __init__(self, base_url, key, *, ttl_s=60.0, fetch=None,
                 clock=time.monotonic):
        self.base_url = base_url
        self._key = key if isinstance(key, str) and key.strip() else None
        self._ttl_s = float(ttl_s)
        self._fetch = fetch or _default_fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._good = None
        self._good_at = None

    def _read(self):
        """Fresh states or ``_FetchFailure`` whose ``why`` is safe to show."""
        if not self._key:
            raise _FetchFailure("no key")
        try:
            status, body = self._fetch(self.base_url + "/api/providers",
                                       {"Authorization": "Bearer " + self._key},
                                       FETCH_TIMEOUT_S)
        except Exception as exc:
            timeout = isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError)
            raise _FetchFailure("timeout" if timeout else type(exc).__name__) from None
        if status != 200:
            raise _FetchFailure(f"http {status}")
        try:
            data = json.loads(body)
            connections = (data.get("connections") if isinstance(data, dict)
                           else data if isinstance(data, list) else None)
        except ValueError:
            connections = None
        if not isinstance(connections, list):
            raise _FetchFailure("bad body")
        if not connections:
            raise _FetchFailure("no providers")
        states = _build(connections, stale=False)
        if not states:
            raise _FetchFailure("bad body")
        return states

    def snapshot(self):
        """States by provider: cached ``ttl_s``, stale-marked up to ``2*ttl_s``."""
        with self._lock:
            now = self._clock()
            if self._good is not None and now - self._good_at < self._ttl_s:
                return dict(self._good)
            try:
                states = self._read()
            except _FetchFailure as failure:
                if self._good is not None and now - self._good_at < 2 * self._ttl_s:
                    return {name: replace(state, stale=True)
                            for name, state in self._good.items()}
                raise OmniRouteUnreachable(
                    "omniroute unreachable: " + str(failure)) from None
            self._good, self._good_at = states, now
            return dict(states)

    def provider_state(self, provider):
        """State of one provider, or None when OmniRoute does not know it."""
        return self.snapshot().get(provider)


def from_env(env=None):
    """Reader configured from the deployment env (gateway base + host key)."""
    base = (clients.effective_gateway_base_url(env) or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    try:
        ttl_s = float((env or os.environ).get(TTL_ENV_VAR))
    except (TypeError, ValueError):
        ttl_s = 60.0
    return OmniRouteState(base or DEFAULT_BASE_URL, _gateway_key(env or os.environ),
                          ttl_s=ttl_s)
