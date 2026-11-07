"""Local inventory identity resolution through Self status entries."""
import json
from pathlib import Path

import pytest

from plangraph.fleet_bus import discovery
from plangraph.fleet_bus.client import (
    Refused, _health, _peer_endpoints, call_tool, load_config,
)
from test_fleet_msg import FakeTransport, _cfg


SELF_STATUS = {"Self": {"PublicKey": "node-self", "Online": True,
                        "DNSName": "own-name"}}


def _self_registry(port=8765):
    return discovery.Registry(
        [discovery.Host(host="coding-vm", node_id="node-self", key_id="kid-self",
                        key_file=Path("self.key"))], port=port)


def test_own_host_in_self_resolves():
    """Vector: the local node under 'Self' resolves to the loopback endpoint.

    The probe pool must bind the registry identity to that endpoint, and the
    derivation must agree with ``Registry.refresh`` endpoint-for-endpoint.
    """
    registry = _self_registry()
    expected = _peer_endpoints(registry, SELF_STATUS)
    assert expected == {"http://127.0.0.1:8765": {"coding-vm": "kid-self"}}

    def probe(endpoint):
        return _health(endpoint, _cfg(),
                       FakeTransport({"status": "ok", "key_id": "kid-self"}),
                       expected.get(endpoint, {}))

    registry.refresh(SELF_STATUS, 0.0, probe)
    assert registry.resolve("coding-vm") == "http://127.0.0.1:8765"
    assert registry.resolve("coding-vm") == next(iter(expected))


def test_own_host_missing_from_status_stays_refused():
    """Vector: no Self entry (and no Peer twin) keeps the own host refused."""
    registry = _self_registry()
    assert _peer_endpoints(registry, {"Peer": {}}) == {}
    registry.refresh({"Peer": {}}, 0.0, lambda endpoint: True)
    with pytest.raises(LookupError):
        registry.resolve("coding-vm")


def test_self_overrides_stale_peer():
    registry = _self_registry()
    status = {**SELF_STATUS, "Peer": {"node-self": {
        "Online": True, "DNSName": "stale-name"}}}
    expected = _peer_endpoints(registry, status)
    assert expected == {"http://127.0.0.1:8765": {"coding-vm": "kid-self"}}
    registry.refresh(status, 0.0, lambda endpoint: endpoint in expected)
    assert registry.resolve("coding-vm") == "http://127.0.0.1:8765"


@pytest.mark.parametrize("local", [None, [], {}, {"PublicKey": 1},
                                  {"PublicKey": ""}, {"PublicKey": " "}])
def test_malformed_self_refuses_peer_twin(local):
    registry = _self_registry()
    status = {"Self": local, "Peer": {"node-self": {
        "Online": True, "DNSName": "stale-name"}}}
    assert _peer_endpoints(registry, status) == {}
    registry.refresh(status, 0.0, lambda endpoint: True)
    with pytest.raises(LookupError):
        registry.resolve("coding-vm")


def test_unprovisioned_host_stays_refused(tmp_path):
    """Invariant: a host absent from the inventory is refused by name."""
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"hosts": [
        {"host": "other", "node_id": "node-other", "key_id": "kid-other",
         "key_file": "other.key"}]}), encoding="utf-8")
    env = {"FLEET_MSG_INVENTORY": str(inventory), "FLEET_MSG_HOST": "coding-vm"}
    with pytest.raises(Refused, match="not provisioned"):
        load_config(env)


def test_peer_with_wrong_identity_stays_refused():
    """Vector: a peer whose /v1/health claims another identity is refused."""
    registry = discovery.Registry(
        [discovery.Host(host="peer-1", node_id="node-peer", key_id="kid-peer",
                        key_file=Path("peer.key"))], port=8765)
    status = {"Peer": {"node-peer": {"Online": True,
                                     "DNSName": "peer-name"}}}
    expected = _peer_endpoints(registry, status)
    assert expected == {"http://peer-name:8765": {"peer-1": "kid-peer"}}

    def probe(endpoint):
        return _health(endpoint, _cfg(),
                       FakeTransport({"status": "ok", "key_id": "kid-OTHER"}),
                       expected.get(endpoint, {}))

    registry.refresh(status, 0.0, probe)
    with pytest.raises(LookupError):
        registry.resolve("peer-1")


class SelfFakeTransport:
    """URL-aware fake: health claims our identity, messages return own rows."""

    instances = []

    def __init__(self, cfg, *, opener=None):
        self.cfg = cfg
        self.calls = []
        SelfFakeTransport.instances.append(self)

    def request(self, url, *, method="GET", payload=None, query=None, auth=True):
        self.calls.append((url, auth))
        if "/v1/health" in url:
            return {"status": "ok", "key_id": {"coding-vm": "kid-self"}}
        return {"rows": [{"msg_id": "f" * 32, "to": "coding-vm",
                          "body": {"text": "own row"}}]}


def test_read_own_rows_with_fake_transport(tmp_path, monkeypatch):
    """Vector: reading own rows works end to end with a fake transport.

    The status snapshot lists the local node under ``Self`` only, exactly the
    live tailscale shape; the read must resolve the loopback keeper and the
    health probe must stay unauthenticated.
    """
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "port": 8765,
        "hosts": [{"host": "coding-vm", "node_id": "node-self",
                   "key_id": "kid-self", "key_file": "self.key"}],
        "agents": {}}), encoding="utf-8")
    (tmp_path / "status.json").write_text(json.dumps(SELF_STATUS),
                                          encoding="utf-8")
    env = {"FLEET_MSG_INVENTORY": str(inventory), "FLEET_MSG_HOST": "coding-vm",
           "FLEET_MSG_SPOOL": str(tmp_path / "spool"),
           "FLEET_MSG_STATUS_FILE": str(tmp_path / "status.json")}
    monkeypatch.setattr("plangraph.fleet_bus.client.HttpTransport",
                        SelfFakeTransport)
    payload = json.loads(call_tool("fleet_msg_read", {"since": 0}, env=env,
                                   log=lambda message: None))
    assert [row["body"] for row in payload["rows"]] == [{"text": "own row"}]
    calls = [call for instance in SelfFakeTransport.instances
             for call in instance.calls]
    assert ("http://127.0.0.1:8765/v1/health", False) in calls
    assert ("http://127.0.0.1:8765/v1/messages", True) in calls
    assert all(auth is False for url, auth in calls if "/v1/health" in url)
