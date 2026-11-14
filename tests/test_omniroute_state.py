"""Hermetic reader regressions; no live acceptance claim."""
import json
import os
import socket
import subprocess
from datetime import datetime, timezone

import pytest

from plangraph.routing_support import host_keys
from plangraph.routing_support.omniroute_state import (
    OmniRouteState, OmniRouteUnreachable, from_env,
)


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("external access forbidden")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(host_keys, "_keys_path", denied)


def payload(*rows):
    return 200, json.dumps({"connections": list(rows)})


def row(**changes):
    return dict(provider="p", isActive=True, **changes)


class Harness:
    def __init__(self, *results, **kwargs):
        self.now, self.calls = 0, []
        self.results = list(results)
        self.reader = OmniRouteState("injected-endpoint", fetch=self.fetch,
                                     clock=lambda: self.now, key="synthetic",
                                     wall_clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
                                     ttl_s=10, **kwargs)

    def fetch(self, *args):
        self.calls.append(args)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_available_and_defensive_snapshot():
    h = Harness(payload(row()))
    first = h.reader.snapshot()
    assert first["p"].available and not first["p"].stale
    first.clear()
    assert h.reader.provider_state("p").available
    assert h.reader.provider_state("missing") is None
    assert len(h.calls) == 1


@pytest.mark.parametrize("bad", [None, {}, [], [None], [{"provider": "p"}],
                                     [row(), {}], [row(testStatus=[])]] )
def test_malformed_never_empty_success(bad):
    h = Harness((200, json.dumps(bad)))
    with pytest.raises(OmniRouteUnreachable, match="bad_snapshot"):
        h.reader.snapshot()


@pytest.mark.parametrize("failure", [(503, "sensitive-body"),
                                    RuntimeError("sensitive-body"), (200, "{")])
def test_failure_is_controlled(failure, capsys):
    h = Harness(failure)
    with pytest.raises(OmniRouteUnreachable) as error:
        h.reader.snapshot()
    assert str(error.value) in {"http_error", "transport_error", "bad_snapshot"}
    assert "sensitive-body" not in str(error.value)
    assert capsys.readouterr() == ("", "")


def test_ttl_stale_expiration_recovery():
    h = Harness(payload(row()), RuntimeError(), RuntimeError(), payload(row()))
    h.reader.snapshot()
    h.now = 9
    assert not h.reader.snapshot()["p"].stale
    h.now = 10
    assert h.reader.snapshot()["p"].stale
    h.results.insert(0, RuntimeError())
    h.now = 20
    assert h.reader.snapshot()["p"].stale
    h.now = 21
    with pytest.raises(OmniRouteUnreachable):
        h.reader.snapshot()
    assert not h.reader.snapshot()["p"].stale
    assert len(h.calls) == 5


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), "bad"])
def test_invalid_ttl(ttl):
    with pytest.raises(ValueError):
        OmniRouteState(None, None, ttl_s=ttl, fetch=lambda *a: None)


@pytest.mark.parametrize("base,key,code", [(None, "synthetic", "no_endpoint"),
                                         ("injected-endpoint", None, "no_key")])
def test_missing_configuration_never_fetches(base, key, code):
    def denied(*args):
        pytest.fail("fetch forbidden")
    reader = OmniRouteState(base, key, fetch=denied)
    with pytest.raises(OmniRouteUnreachable, match=code):
        reader.snapshot()


def test_connections_keep_winning_row_metadata():
    limited = row(rateLimitedUntil="2999-01-01T00:00:00Z")
    for rows in [(limited, row()), (row(), limited)]:
        state = Harness(payload(*rows)).reader.snapshot()["p"]
        assert state.available and state.until == "" and state.reason == ""
    state = Harness(payload(limited)).reader.snapshot()["p"]
    assert not state.available and state.reason == "rate_limited"
    state = Harness(payload(dict(provider="p", isActive=False,
                                 lastErrorType="sensitive-body"))).reader.snapshot()["p"]
    assert not state.available and state.reason == "inactive"


@pytest.mark.parametrize("status", ["error", "failed", "unavailable"])
def test_unavailable_status(status):
    state = Harness(payload(row(testStatus=status))).reader.snapshot()["p"]
    assert not state.available and state.reason == "inactive" and not state.stale


def test_from_env_injected_resolution():
    calls = []
    def resolve(env):
        calls.append(env)
        return None
    # Explicit env mappings do not inherit the suite's isolated config root.
    reader = from_env({"XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]},
                      key_resolver=resolve, fetch=lambda *a: pytest.fail("fetch"))
    with pytest.raises(OmniRouteUnreachable, match="no_endpoint"):
        reader.snapshot()
    assert not calls
    reader = from_env({"OMNIROUTE_BASE_URL": "https://gw.example/v1"},
                      key_resolver=resolve, fetch=lambda *a: pytest.fail("fetch"))
    assert reader.base_url == "https://gw.example"
    with pytest.raises(OmniRouteUnreachable, match="no_key"):
        reader.snapshot()
