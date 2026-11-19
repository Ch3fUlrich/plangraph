"""Opt-in provider-state reader; no routing caller is activated here.

Only successful snapshots are cached. Refresh failures expose stale data for
at most twice the TTL since success, then raise a controlled unreachable code.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .clients import effective_gateway_base_url
from .opencode_render import (
    _NoManagementRedirect, gateway_management_base_url, valid_gateway_endpoint,
)
from plangraph.spawner import gateway_witness

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
                or status not in ("ok", "success", "error", "failed", "unavailable")):
            raise ValueError
        until = row.get("rateLimitedUntil") or ""
        limited = False
        if until:
            stamp = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
            limited = stamp > now
            until = stamp.isoformat()
        available = active and status in ("ok", "success") and not row.get("errorCode")
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
            key = (key_resolver or gateway_witness.load_token)(source)
        except (Exception, SystemExit):
            pass
    kwargs.setdefault("ttl_s", source.get(TTL_ENV_VAR, 60))
    kwargs.setdefault("principal", source.get('PLANGRAPH_PRINCIPAL') or 'unknown')
    return OmniRouteState(base, key, **kwargs)
