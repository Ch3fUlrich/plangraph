"""Hermetic reader regressions; no live acceptance claim."""
import json
import os
import socket
import subprocess
from datetime import datetime, timezone

import pytest

from plangraph.routing_support import host_keys, omniroute_state
from plangraph.routing_support.omniroute_state import (
    OmniRouteAuthRefused, OmniRouteState, OmniRouteUnreachable, from_env,
)
from plangraph.spawner import gateway_witness


@pytest.fixture(autouse=True)
def deny_external(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        pytest.fail("external access forbidden")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(host_keys, "_keys_path", denied)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PLANGRAPH_GATEWAY_LOG_TOKEN", raising=False)
    monkeypatch.delenv("PLANGRAPH_GATEWAY_LOG_TOKEN_FILE", raising=False)


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


@pytest.mark.parametrize('status', [401, 403])
def test_auth_refused_never_uses_stale_health(status):
    from plangraph.routing_support.omniroute_state import OmniRouteAuthRefused
    h = Harness(payload(row()), (status, 'sensitive-body'), RuntimeError(), payload(row()),
                principal='reviewer')
    h.reader.snapshot()
    h.now = 10
    with pytest.raises(OmniRouteAuthRefused, match='auth_refused') as error:
        h.reader.snapshot()
    assert 'synthetic' not in str(error.value)
    assert 'sensitive-body' not in str(error.value)
    with pytest.raises(OmniRouteUnreachable, match='transport_error'):
        h.reader.snapshot()
    assert not h.reader.snapshot()['p'].stale
    assert len(h.calls) == 4


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


def test_from_env_uses_management_credential_not_client_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    key_file = home / ".config" / "plangraph" / "omniroute-mgmt.key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("mgmt-from-home\n", encoding="utf-8")
    key_file.chmod(0o600)

    monkeypatch.setattr(host_keys, "_gateway_key", lambda env: "client-secret")
    monkeypatch.setattr(omniroute_state, "_gateway_key", lambda env: "client-secret", raising=False)

    calls = []

    def fetch(url, headers, timeout):
        calls.append((url, headers, timeout))
        return payload(row())

    reader = from_env({"OMNIROUTE_BASE_URL": "https://gw.example/v1", "HOME": str(home)},
                      fetch=fetch)
    assert reader.snapshot()["p"].available
    assert calls == [("https://gw.example/api/providers",
                      {"Authorization": "Bearer mgmt-from-home"}, 8.0)]

    # Env token and explicit token file also route through load_token, never client key.
    calls.clear()
    reader = from_env({"OMNIROUTE_BASE_URL": "https://gw.example/v1",
                       gateway_witness.TOKEN_ENV: "mgmt-from-env"},
                      fetch=fetch)
    assert reader.snapshot()["p"].available
    assert calls[0][1] == {"Authorization": "Bearer mgmt-from-env"}

    explicit_file = tmp_path / "explicit.key"
    explicit_file.write_text("mgmt-from-file\n", encoding="utf-8")
    explicit_file.chmod(0o600)
    calls.clear()
    reader = from_env({"OMNIROUTE_BASE_URL": "https://gw.example/v1",
                       gateway_witness.TOKEN_FILE_ENV: str(explicit_file)},
                      fetch=fetch)
    assert reader.snapshot()["p"].available
    assert calls[0][1] == {"Authorization": "Bearer mgmt-from-file"}

    # Injected management resolver + injected client resolver: management value wins.
    calls.clear()
    reader = from_env({"OMNIROUTE_BASE_URL": "https://gw.example/v1"},
                      key_resolver=lambda env: "mgmt-injected", fetch=fetch)
    assert reader.snapshot()["p"].available
    assert calls[0][1] == {"Authorization": "Bearer mgmt-injected"}


def test_from_env_no_management_credential_never_sends_client_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(host_keys, "_gateway_key", lambda env: "client-secret")
    monkeypatch.setattr(omniroute_state, "_gateway_key", lambda env: "client-secret", raising=False)

    calls = []

    def fetch(*args):
        calls.append(args)
        pytest.fail("client key must never be sent to /api/providers")

    reader = from_env({"OMNIROUTE_BASE_URL": "https://gw.example/v1", "HOME": str(home)},
                      fetch=fetch)
    with pytest.raises(OmniRouteUnreachable, match="^no_key$"):
        reader.snapshot()
    assert not calls

    # Insecure permissions on omniroute-mgmt.key are rejected without falling back to client key.
    bad_key = home / ".config" / "plangraph" / "omniroute-mgmt.key"
    bad_key.parent.mkdir(parents=True)
    bad_key.write_text("mgmt-world-readable\n", encoding="utf-8")
    bad_key.chmod(0o644)
    reader = from_env({"OMNIROUTE_BASE_URL": "https://gw.example/v1", "HOME": str(home)},
                      fetch=fetch)
    with pytest.raises(OmniRouteUnreachable, match="^no_key$"):
        reader.snapshot()
    assert not calls


@pytest.mark.parametrize("status", [401, 403])
def test_from_env_management_401_403_raises_auth_refused(status):
    reader = from_env(
        {"OMNIROUTE_BASE_URL": "https://gw.example/v1",
         gateway_witness.TOKEN_ENV: "mgmt-secret",
         "PLANGRAPH_PRINCIPAL": "implement"},
        fetch=lambda *args: (status, "unauthorized"),
    )
    with pytest.raises(OmniRouteAuthRefused, match="auth_refused") as error:
        reader.snapshot()
    assert "mgmt-secret" not in str(error.value)
