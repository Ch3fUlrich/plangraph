"""Per-host leg truth: served or not with the reason, placement skips unserved legs
(LE-WS-OPENCODE-GO-STATE-UNPROBED)."""
from __future__ import annotations

import subprocess

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.routing_support import leg_state
from plangraph.routing_support.leg_state import DiscoveryFailed, host_served_legs, served_digest
from plangraph.spawner import overflow

GLM = next(leg for leg in DEFAULT_LEGS if leg.id == "opencode-go-glm-5.3-flash")
DEEPSEEK = next(leg for leg in DEFAULT_LEGS if leg.id == "opencode-go-deepseek-v4-flash")
PORT_ERROR = ("Managed service port 49374 on 127.0.0.1 is already in use by PID 2808090 "
              "(opencode serve --service)")


def test_fixture_host_whose_list_lacks_a_leg_shows_not_served_with_the_reason():
    listing = ["opencode-go/deepseek-v4.1-flash"]   # the workstation lists no opencode-go/glm
    state = host_served_legs([GLM, DEEPSEEK], listing)
    assert state[DEEPSEEK.id] == dict(served=True, reason="listed")
    assert state[GLM.id] == dict(served=False, reason="not_listed: " + GLM.model)


def test_failed_tiny_call_overrides_a_listed_model():
    state = host_served_legs([GLM], [GLM.model], probe_failures={GLM.id: "exit 1"})
    assert state[GLM.id] == dict(served=False, reason="exit 1")


def test_port_conflict_error_maps_to_its_own_reason():
    assert leg_state.harness_error_reason(PORT_ERROR) == "managed_service_port_conflict"
    assert leg_state.harness_error_reason("exit 1") is None
    state = host_served_legs([GLM], DiscoveryFailed("managed_service_port_conflict"))
    assert state[GLM.id]["reason"] == "managed_service_port_conflict"


def test_discovery_stderr_with_the_port_conflict_is_its_own_reason(monkeypatch, tmp_path):
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PLANGRAPH_OPENCODE_BIN", str(binary))

    def run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr=PORT_ERROR)
    monkeypatch.setattr(subprocess, "run", run)
    failed = leg_state.LegAdapter.standalone_models()
    assert isinstance(failed, DiscoveryFailed) and failed.reason == "managed_service_port_conflict"


def test_missing_harness_is_its_own_host_reason():
    state = host_served_legs([GLM], DiscoveryFailed("harness_not_found opencode /usr/bin"))
    assert state[GLM.id] == dict(served=False, reason="harness_missing")


def test_digest_line_lists_legs_served_per_host():
    hosts = {"ws": host_served_legs([GLM, DEEPSEEK], ["opencode-go/deepseek-v4.1-flash"]),
             "coding.vm": host_served_legs([GLM, DEEPSEEK], [GLM.model, DEEPSEEK.model])}
    line = served_digest(hosts)
    assert line == ("legs served per host: coding.vm 2/2; "
                    f"ws 1/2 (not served: {GLM.id} not_listed: {GLM.model})")


def _row(**extra):
    return dict(ssh="h", ok=True, fresh=True, live_runs=0, mem_available_mb=9000, role_mb=1000,
                floor_mb=1000, disk_free_gb=100, **extra)


def test_placement_skips_a_host_that_does_not_serve_the_leg():
    served = {GLM.id: dict(served=True, reason="listed")}
    unserved = {GLM.id: dict(served=False, reason="not_listed: " + GLM.model)}
    rows = {"a": _row(served_legs=unserved), "b": _row(served_legs=served)}
    decision = overflow.decide(0, 1000, 1000, rows, True, local_refusal="local full", leg=GLM.id)
    assert decision.host == "b"
    only = overflow.decide(0, 1000, 1000, {"a": rows["a"]}, True, local_refusal="local full", leg=GLM.id)
    assert only.host is None and "a: leg_not_served: " + GLM.id in only.reason
    # a leg the host says nothing about stays placeable
    assert overflow.decide(0, 1000, 1000, {"a": rows["a"]}, True, local_refusal="local full",
                           leg=DEEPSEEK.id).host == "a"
