"""Hermetic tests for routing_support.omniroute_state (SPAWNER-LEG-STATE-FROM-OMNIROUTE slice 1).

Fake fetch + fake clock only: no network, no real key file (every ``from_env``
case points ``AUTOOS_ROOT`` at an empty tmp_path).
"""
from __future__ import annotations

import json

import pytest

from plangraph.routing_support.omniroute_state import (
    OmniRouteState,
    OmniRouteUnreachable,
    ProviderState,
    from_env,
)

KEY = "fake-gateway-key-XYZ789"


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeFetch:
    """Queue of (status, body) or exception instances; records every call.

    The last entry repeats once the queue would drain, so a "then it keeps
    failing" case needs no extra plumbing.
    """

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def payload(*connections):
    return (200, json.dumps({"connections": list(connections)}).encode("utf-8"))


def reader(results, *, key=KEY, ttl=60.0, clock=None):
    fetch = FakeFetch(*results)
    state = OmniRouteState("http://omni:1", key, ttl_s=ttl, fetch=fetch,
                           clock=clock or FakeClock())
    return state, fetch


def test_available_provider():
    state, fetch = reader([payload({"provider": "anthropic", "isActive": True,
                                   "testStatus": "ok"})])
    assert state.snapshot() == {
        "anthropic": ProviderState("anthropic", True, "", "", False)}
    assert fetch.calls[0][0] == "http://omni:1/api/providers"
    assert fetch.calls[0][1] == {"Authorization": "Bearer " + KEY}
    assert fetch.calls[0][2] == 8.0


def test_is_active_false_reason_inactive():
    state, _ = reader([payload({"provider": "p", "isActive": False})])
    assert state.snapshot()["p"] == ProviderState("p", False, "inactive", "", False)


def test_rate_limited_until_future_and_past():
    future = {"provider": "p", "isActive": True,
              "rateLimitedUntil": "2999-01-01T00:00:00Z"}
    state, _ = reader([payload(future)])
    assert state.snapshot()["p"] == ProviderState(
        "p", False, "rate_limited", "2999-01-01T00:00:00Z", False)
    past = {"provider": "q", "isActive": True,
            "rateLimitedUntil": "2000-01-01T00:00:00Z"}
    state, _ = reader([payload(past)])
    assert state.snapshot()["q"] == ProviderState(
        "q", True, "", "2000-01-01T00:00:00Z", False)


def test_two_connections_one_active():
    rows = [{"provider": "p", "isActive": False},
            {"provider": "p", "isActive": True}]
    state, _ = reader([payload(*rows)])
    assert state.snapshot()["p"] == ProviderState("p", True, "", "", False)


def test_http_403_raises_without_key_in_message():
    state, _ = reader([(403, b"denied")])
    with pytest.raises(OmniRouteUnreachable) as excinfo:
        state.snapshot()
    assert "http 403" in str(excinfo.value)
    assert KEY not in str(excinfo.value)
    assert KEY not in repr(excinfo.value.args)


def test_failure_stale_within_2ttl_then_raise_beyond():
    clock = FakeClock()
    state, fetch = reader([payload({"provider": "p", "isActive": True}),
                           (500, b"boom")], clock=clock)
    assert state.snapshot()["p"].available is True
    clock.now = 90.0  # >= ttl, still < 2*ttl
    assert state.snapshot()["p"] == ProviderState("p", True, "", "", True)
    clock.now = 125.0  # beyond 2*ttl: the stale cache is gone, fail visible
    with pytest.raises(OmniRouteUnreachable, match="http 500"):
        state.snapshot()
    assert len(fetch.calls) == 3


def test_cache_hit_within_ttl_skips_fetch():
    clock = FakeClock()
    state, fetch = reader([payload({"provider": "p", "isActive": True})],
                          clock=clock)
    state.snapshot()
    clock.now = 59.0
    assert state.snapshot()["p"] == ProviderState("p", True, "", "", False)
    assert len(fetch.calls) == 1


def test_zero_connections_raises_no_providers():
    state, _ = reader([payload()])
    with pytest.raises(OmniRouteUnreachable, match="no providers"):
        state.snapshot()


def test_bad_json_and_bad_shape_raise_bad_body():
    state, _ = reader([(200, b"{not json")])
    with pytest.raises(OmniRouteUnreachable, match="bad body"):
        state.snapshot()
    state, _ = reader([(200, json.dumps({"nope": []}).encode("utf-8"))])
    with pytest.raises(OmniRouteUnreachable, match="bad body"):
        state.snapshot()


def test_no_key_raises_without_calling_fetch():
    state, fetch = reader([payload({"provider": "p", "isActive": True})], key=None)
    with pytest.raises(OmniRouteUnreachable, match="no key"):
        state.snapshot()
    assert fetch.calls == []


def test_exception_from_fetch_uses_class_name():
    state, _ = reader([RuntimeError("kaboom")])
    with pytest.raises(OmniRouteUnreachable, match="RuntimeError"):
        state.snapshot()


def test_provider_state_lookup():
    state, _ = reader([payload({"provider": "p", "isActive": True})])
    assert state.provider_state("p") == ProviderState("p", True, "", "", False)
    assert state.provider_state("ghost") is None


def test_from_env_strips_v1_and_falls_back(tmp_path):
    env = {"OMNIROUTE_BASE_URL": "http://h:1/v1", "AUTOOS_ROOT": str(tmp_path)}
    assert from_env(env).base_url == "http://h:1"
    env = {"AUTOOS_ROOT": str(tmp_path)}
    assert from_env(env).base_url == "http://127.0.0.1:20128"


def test_from_env_ttl_env_var_overrides(tmp_path):
    env = {"OMNIROUTE_BASE_URL": "http://h:1", "AUTOOS_ROOT": str(tmp_path),
           "PLANGRAPH_OMNIROUTE_STATE_TTL_S": "2.5"}
    assert from_env(env)._ttl_s == 2.5
