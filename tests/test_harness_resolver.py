"""One harness resolver: PATH-stripped spawns, named refusal, leg state, seat reason, watchdog
(LE-SPAWN-HARNESS-LOOKUP-BY-PATH-REPORTED-AS-NO-VERDICT)."""
from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.routing_support import clients, leg_state, resolver
from plangraph.routing_support.clients import resolve_harness_binary

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _binary(directory, name="opencode"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bare_unit(tmp_path, monkeypatch):
    """A systemd-run user unit: system PATH only, a HOME with an nvm install, no overrides."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("PLANGRAPH_OPENCODE_BIN", "NVM_DIR", "PLANGRAPH_HOST_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PATH", SYSTEM_PATH)
    monkeypatch.setenv("INVOCATION_ID", "unit")
    monkeypatch.setattr(clients, "_warned_path_fallback", set())
    return home


def test_spawn_with_the_harness_dir_off_path_resolves_through_the_resolver(bare_unit):
    binary = _binary(bare_unit / ".nvm/versions/node/v24.19.0/bin")
    assert shutil_which_system("opencode") is None   # the bare PATH alone cannot find it
    found = resolve_harness_binary("opencode", {"PATH": SYSTEM_PATH, "HOME": str(bare_unit), "INVOCATION_ID": "u"})
    assert found.path == str(binary) and found.refusal is None


def shutil_which_system(name):
    import shutil
    return shutil.which(name, path=SYSTEM_PATH)


def test_host_table_wins_over_the_search_and_needs_no_warning(bare_unit, tmp_path, caplog):
    binary = _binary(tmp_path / "pinned")
    config = tmp_path / "config/plangraph"
    config.mkdir(parents=True)
    (config / "hosts.json").write_text(json.dumps(
        {"self": "h1", "hosts": [{"id": "h1", "harness_bins": {"opencode": str(binary)}}]}))
    found = resolve_harness_binary("opencode", {"PATH": SYSTEM_PATH, "HOME": str(bare_unit)})
    assert (found.path, found.source) == (str(binary), "host_table")
    assert "harness_path_fallback" not in caplog.text


def test_path_search_answers_with_a_logged_warning(bare_unit, caplog):
    _binary(bare_unit / ".local/bin")
    found = resolve_harness_binary("opencode", {"PATH": SYSTEM_PATH, "HOME": str(bare_unit), "INVOCATION_ID": "u"})
    assert found.source == "search"
    assert "harness_path_fallback opencode" in caplog.text


def test_absent_binary_is_refused_by_name_with_the_searched_paths(bare_unit):
    found = resolve_harness_binary("opencode", {"PATH": SYSTEM_PATH, "HOME": str(bare_unit)})
    assert found.path is None
    assert found.refusal.startswith("harness_not_found opencode ")
    assert "/usr/bin" in found.refusal


def test_discovery_without_the_binary_is_harness_missing_not_unreachable(bare_unit, tmp_path):
    discovered = leg_state.LegAdapter.standalone_models()
    assert isinstance(discovered, leg_state.DiscoveryFailed)
    assert discovered.reason.startswith("harness_not_found opencode ")

    class Store:
        def leg_status(self, *a, **k):
            return dict(state="ok", reason="ok", evidence=None)
    reader = leg_state.LegAdapter(Store(), SimpleNamespace(snapshot=lambda: {}), enabled=True)
    status = reader("opencode-go/glm-5.3-flash")
    assert status["state"] == "down"
    assert status["reason"].startswith("harness_missing: opencode")


def test_host_skip_reason_names_a_missing_opencode_not_an_unserved_model(bare_unit):
    row = dict(model="opencode-go/glm-5.3-flash", harness="opencode")
    reason = resolver.host_skip_reason(row, probe=lambda env: frozenset())
    assert reason.startswith("harness_not_found opencode ")


def test_seat_reason_carries_the_refused_runs_cause():
    from plangraph.spawner.seat_artifact import sync_seat
    cause = "harness_not_found opencode /usr/bin:/bin"
    record = dict(role="review", status="failed", chosen_leg=None, ended=1,
                  attempts=[dict(status="skipped", skip_reason=cause)],
                  plan=dict(seat_coverage=[dict(leg="x", seat_group="g")]))
    sync_seat(record)
    assert record["plan"]["seat_coverage"][0]["reason"] == cause


def _spawn_claude(tmp_path, monkeypatch, home):
    """SpawnerRunner.spawn of the claude harness from a bare unit environment; returns (exe the factory got, job)."""
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", SYSTEM_PATH)
    monkeypatch.setenv("INVOCATION_ID", "unit")
    seen = {}

    class Stop(Exception):
        pass

    async def factory(exe, *args, **kwargs):
        seen["exe"] = exe
        raise Stop

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    job = None
    try:
        job = asyncio.run(runner.spawn("review this", "claude-haiku-5-5", role="review", client="claude",
                                       cwd=str(tmp_path)))
    except Stop:
        pass
    return seen.get("exe"), job


def test_spawn_from_a_bare_unit_executes_the_harness_found_off_path(bare_unit, tmp_path, monkeypatch):
    binary = _binary(bare_unit / ".nvm/versions/node/v24.19.0/bin", "claude")
    assert shutil_which_system("claude") is None
    exe, _ = _spawn_claude(tmp_path, monkeypatch, bare_unit)
    assert exe == str(binary)   # the process factory received the resolver's absolute path


def test_spawn_without_the_harness_is_refused_by_name_before_any_exec(bare_unit, tmp_path, monkeypatch):
    exe, job = _spawn_claude(tmp_path, monkeypatch, bare_unit)
    assert exe is None, "nothing may be executed"
    assert job.status == "failed" and job.stderr.startswith("harness_not_found claude ")


def test_mimo_admission_shortcut_never_hides_a_missing_opencode(bare_unit):
    # mimo ids are admitted by render without a model listing; the binary check still comes first
    row = dict(model="mimo/mimo-v2.6-pro", harness="opencode")
    assert resolver.host_skip_reason(row).startswith("harness_not_found opencode ")
    _binary(bare_unit / ".local/bin")
    assert not (resolver.host_skip_reason(row) or "").startswith("harness_not_found")
