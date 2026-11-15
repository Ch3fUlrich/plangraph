"""Offline inputs shared by the prompt-transport invariant tests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture
def transport_sandbox(monkeypatch, tmp_path):
    from plangraph.planner import legs
    from plangraph.routing_support import catalog_db, resolver

    platform = {name.upper() for name in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")}
    for name in list(os.environ):
        if name.upper() not in platform:
            monkeypatch.delenv(name)
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        directory = tmp_path / name.lower()
        directory.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(FIXTURES / "prompt_transport_policy.json"))
    rows = json.loads((FIXTURES / "prompt_transport_legs.json").read_text(encoding="utf-8"))
    fixed = tuple(legs.Leg(**{**row, "windows": tuple(row["windows"]),
                             "roles": tuple(row["roles"])}) for row in rows["legs"])
    monkeypatch.setattr(legs, "DEFAULT_LEGS", fixed)
    monkeypatch.setattr(legs, "EXPLICIT_LEGS", ())

    def forbidden(*args, **kwargs):
        pytest.fail("transport test attempted a live catalog, probe, network or process")

    for name in ("get_catalog_models", "served_models", "_served_models_real",
                 "_probe_served_models"):
        monkeypatch.setattr(resolver, name, forbidden)
    monkeypatch.setattr(catalog_db.CatalogDB, "__init__", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert Path(sys.executable).is_absolute()
    return json.loads((FIXTURES / "prompt_transport_contract.json").read_text(encoding="utf-8"))
