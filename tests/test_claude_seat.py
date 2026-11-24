"""Hermetic argv contracts for the lean Claude review harness."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from plangraph.routing_support import claude_seat
from plangraph.routing_support.claude_seat import (
    REVIEW_DENIED,
    REVIEW_TOOLS,
    claude_access_args,
)
from plangraph.routing_support.clients import build_command


@pytest.mark.parametrize("role", ["review", "implement", "orchestrate"])
@pytest.mark.parametrize("mode", ["tools", "inline-only", "prompt-only"])
def test_role_mode_argv(role, mode, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = build_command("p", "claude/test", role=role, client="claude",
                        tool_mode=mode)
    prefix = ["claude", "-p", "--model", "claude/test", "--output-format", "stream-json", "--verbose"]
    args = claude_access_args(role, mode)
    assert cmd == prefix + args
    if role != "review":
        assert args == ["--permission-mode", "plan"]  # V5: exact old argv
        return
    # V1: permission strings remain exactly the old layer, never broadened.
    assert args[:8] == ["--permission-mode", "default", "--setting-sources", "user",
                        "--allowedTools", REVIEW_TOOLS, "--disallowedTools", REVIEW_DENIED]
    assert args[args.index("--tools") + 1] == ("" if mode == "prompt-only" else "Read,Grep,Glob,Bash")
    assert "--strict-mcp-config" in args
    assert "--disable-slash-commands" in args
    config = Path(args[args.index("--mcp-config") + 1])
    assert config.is_absolute() and config.is_file()
    assert json.loads(config.read_text()) == {"mcpServers": {}}


@pytest.mark.parametrize("client", [None, "claude"])
@pytest.mark.parametrize("known", [True, False])
def test_clients_derives_leg_mode(monkeypatch, client, known):
    from dataclasses import replace

    from plangraph.planner import legs

    leg = replace(legs.DEFAULT_LEGS[-1], model="claude/test", tool_mode="prompt-only")
    monkeypatch.setattr(legs, "DEFAULT_LEGS", (leg,) if known else ())
    cmd = build_command("p", "claude/test", role="review", client=client,
                        gateway=False)
    assert cmd[cmd.index("--tools") + 1] == ("" if known else "Read,Grep,Glob,Bash")


def test_clients_explicit_mode_overrides_leg(monkeypatch):
    from dataclasses import replace

    from plangraph.planner import legs

    monkeypatch.setattr(legs, "DEFAULT_LEGS", (
        replace(legs.DEFAULT_LEGS[-1], tool_mode="inline-only"),))
    cmd = build_command("p", "claude-haiku-5-5", role="review", client="claude",
                        tool_mode="tools")
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,Bash"


def test_gateway_prompt_only(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"providers":{"fixture":{}}}')
    cmd = build_command("/not-a-command", "test/unregistered", role="review",
                        client="opencode", tool_mode="prompt-only")
    assert cmd[0] == sys.executable
    config = json.loads(Path(cmd[2]).read_text())
    assert cmd[3:] == ["opencode", "run", "--standalone", "--model",
                       "test/unregistered", "--agent", "lean-review"]
    agent = config["agents"]["lean-review"]
    assert agent["steps"] == 1  # OpenCode final step removes tools entirely.
    assert agent["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
    assert {p["action"] for p in config["policies"]} == {
        "tool.use", "mcp.use", "skill.use", "plugin.use"}
    assert all(p["effect"] == "deny" and p["resource"] == "*" for p in config["policies"])
    assert config["providers"] == {"fixture": {}}
    assert "/not-a-command" not in cmd  # prompt goes over stdin, not slash dispatch


def test_package_resource():
    import tomllib
    from importlib.resources import files

    assert json.loads(files("plangraph").joinpath("config/empty-mcp.json").read_text()) == {"mcpServers": {}}
    packaging = tomllib.loads(Path("pyproject.toml").read_text())
    assert "config/*.json" in packaging["tool"]["setuptools"]["package-data"]["plangraph"]


def test_sonnet_keeps_worker_cargo_target(tmp_path, monkeypatch):
    from plangraph.planner.legs import EXPLICIT_LEGS, REVIEW_SONNET
    from plangraph.routing_support import claude_seat

    monkeypatch.setattr(claude_seat, "sandbox_available", lambda _: True, raising=False)
    leg = next(leg for leg in EXPLICIT_LEGS if leg.id == REVIEW_SONNET)
    argv = claude_access_args("review")
    target = tmp_path / "target"
    env, run_dirs = {"CARGO_TARGET_DIR": str(target)}, {}
    claude_seat.configure_review_tests(argv, env, leg, "review", run_dirs, "fixture",
                           review_target=str(target))

    sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
    assert Path(env["CARGO_TARGET_DIR"]) == target
    assert Path(env["CARGO_BUILD_BUILD_DIR"]) == target
    assert Path(env["TMPDIR"]) == run_dirs["fixture"]
    assert run_dirs["fixture"].parent == target.parent
    assert set(sandbox["filesystem"]["allowWrite"]) == {str(target), env["TMPDIR"]}


def test_unknown_model_tools(monkeypatch):
    from plangraph.routing_support import clients

    def reject(_):
        raise ValueError("unregistered")
    monkeypatch.setattr(clients, "detect_client_for_model", reject)
    assert build_command("p", "legacy-alias", role="review") == [
        "opencode", "run", "--standalone", "--model", "legacy-alias"]


def test_default_review_modes():
    from plangraph.planner.legs import DEFAULT_LEGS

    assert any(leg.tool_mode == "inline-only" for leg in DEFAULT_LEGS)
    leg = next(leg for leg in DEFAULT_LEGS if leg.harness == "claude")
    cmd = build_command("p", leg.model, client="claude", role="review")
    assert cmd[cmd.index("--tools") + 1] == ""


def test_sonnet_without_sandbox_is_read_only(tmp_path, monkeypatch):
    from plangraph.planner.legs import EXPLICIT_LEGS, REVIEW_SONNET
    from plangraph.routing_support import claude_seat

    monkeypatch.setattr(claude_seat, "sandbox_available", lambda _: False, raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "fixture-host")
    leg = next(leg for leg in EXPLICIT_LEGS if leg.id == REVIEW_SONNET)
    argv = claude_access_args("review")
    env, run_dirs = {}, {}
    event = claude_seat.configure_review_tests(argv, env, leg, "review", run_dirs, "fixture",
                                               review_target=tmp_path / "target")
    for flag in ("--allowedTools", "--tools"):
        assert argv[argv.index(flag) + 1] == "Read,Grep,Glob"
    assert argv[argv.index("--disallowedTools") + 1] == "Write,Edit,Bash,mcp__*"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--settings" not in argv
    assert "--restricted" in argv
    assert "TESTS: none: sandbox unavailable on fixture-host" in argv[-1]
    assert event == {"event": "sandbox_unavailable", "host": "fixture-host", "python_job_id": "fixture"}
    assert env == run_dirs == {}
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("exit_code", [0, 1, None], ids=["pass", "fail", "timeout"])
def test_sandbox_smoke_result_is_cached(tmp_path, monkeypatch, exit_code):
    from plangraph.routing_support import claude_seat

    monkeypatch.setattr(claude_seat.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    env = {"PATH": str(tmp_path)}
    for name in ("bwrap", "socat"):
        binary = tmp_path / name
        binary.touch()
        binary.chmod(0o700)
    calls = []

    def smoke(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] > 0
        assert kwargs["cwd"] == "/"
        if exit_code is None:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, exit_code)

    monkeypatch.setattr(subprocess, "run", smoke)
    assert claude_seat.sandbox_available(env) is (exit_code == 0)
    assert claude_seat.sandbox_available(env) is (exit_code == 0)
    assert len(calls) == 1
    assert calls[0][0] == str(tmp_path / "bwrap")

    (tmp_path / "socat").unlink()
    assert claude_seat.sandbox_available(env) is False
    assert len(calls) == 1


@pytest.mark.parametrize("command, allowed", [
    ("pytest -q tests/test_claude_seat.py", True),
    ("python3 -m pytest -q tests/test_claude_seat.py::test_package_resource", True),
    ("pytest -q tests/../tests/test_claude_seat.py", False),
    ("pytest -q tests\\test_claude_seat.py", False),
    ("pytest -q /opt/outside/test_x.py", False),
])
def test_review_pytest_paths_are_judged_by_text_not_platform_path_text(
        command, allowed, tmp_path, monkeypatch):
    # Windows renders Path text with backslashes; the policy regex is slash-only (CI Windows job).
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_claude_seat.py").write_text("")

    class BackslashPath(type(tmp_path)):
        def __str__(self):
            return super().__str__().replace("/", "\\")

    monkeypatch.setattr(claude_seat, "Path", BackslashPath)
    assert claude_seat._review_command(command, tmp_path) is allowed


def _configure_seat(tmp_path, monkeypatch, configured, outside_temp):
    from plangraph.planner.legs import EXPLICIT_LEGS, REVIEW_SONNET
    from plangraph.routing_support import claude_seat
    from plangraph.spawner import host_ready_source

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(configured))
    if outside_temp:  # pytest's tmp_path itself sits under the system temp directory
        monkeypatch.setattr(host_ready_source, "_under_system_temp", lambda _: False)
    monkeypatch.setattr(claude_seat, "sandbox_available", lambda _: True, raising=False)
    leg = next(leg for leg in EXPLICIT_LEGS if leg.id == REVIEW_SONNET)
    argv, env, run_dirs, leases = claude_access_args("review"), {}, {}, {}
    from plangraph.spawner.host_ready_source import review_cargo_slot
    claude_seat.configure_review_tests(argv, env, leg, "review", run_dirs, "job1", leases=leases,
                                       allocate_slot=review_cargo_slot)
    return home, argv, env, run_dirs, leases


def test_review_seat_without_worker_target_builds_in_the_capped_pool(tmp_path, monkeypatch):
    """The seat's slot is in the pool that cleanup and hygiene enumerate, and it holds its lease."""
    import fcntl

    from plangraph.spawner import host_ready

    configured = tmp_path / "cache/cargo-target"
    _, argv, env, run_dirs, leases = _configure_seat(tmp_path, monkeypatch, configured, True)
    pool = tmp_path / "cache/cargo-target-pool"
    target = Path(env["CARGO_TARGET_DIR"])
    assert target == pool / "slot-review-job1" / "target"
    assert Path(env["PLANGRAPH_CARGO_TARGET_DIR"]) == target
    assert Path(env["TMPDIR"]).parent == target.parent
    assert run_dirs["job1"] == target.parent
    sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
    assert sandbox["filesystem"]["allowWrite"] == [str(target.parent)]
    # The same slot list that admission cleanup and the Rust cap trim enumerate.
    assert target.parent in host_ready._shared_cache_roots()
    # Hygiene's exclusive lease on the slot is refused while the seat runs.
    probe = open(str(target.parent) + ".plangraph-lock", "a+b")
    with pytest.raises(BlockingIOError):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    leases["job1"].close()
    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    probe.close()


def test_review_seat_never_builds_under_tmp_and_cleanup_enumerates_where_it_does(tmp_path, monkeypatch):
    """A shared target configured under /tmp is not used for review slots; admission cleanup finds them."""
    from plangraph.spawner import host_ready

    home, _, env, _, leases = _configure_seat(
        tmp_path, monkeypatch, "/tmp/plangraph-review-shared-cargo-target", False)
    target = Path(env["CARGO_TARGET_DIR"])
    assert target == home / ".cache/plangraph/cargo-target-pool/slot-review-job1/target"
    # Not beside the configured /tmp directory, wherever pytest put its own temp root.
    assert "plangraph-review-shared-cargo-target-pool" not in str(target)
    assert target.parent in host_ready._shared_cache_roots()
    leases["job1"].close()


def test_only_the_allocated_review_slot_is_returned_and_dropped(tmp_path):
    from plangraph.spawner.launch_spec import allocated_review_slot, drop_review_slot

    slot = tmp_path / "cargo-target-pool" / "slot-review-job1"
    (slot / "target").mkdir(parents=True)
    env = {"CARGO_TARGET_DIR": str(slot / "target")}
    assert allocated_review_slot(str(slot), env) == str(slot)
    # What the allocator did not record is never claimed: no record, another path, a plain dir.
    assert allocated_review_slot(None, env) is None
    assert allocated_review_slot(str(tmp_path / "other"), env) is None
    plain = tmp_path / "plain"
    (plain / "target").mkdir(parents=True)
    assert allocated_review_slot(str(plain), {"CARGO_TARGET_DIR": str(plain / "target")}) is None
    drop_review_slot(None)
    assert plain.exists() and slot.exists()
    lock = Path(str(slot) + ".plangraph-lock")
    lock.write_text("")
    drop_review_slot(allocated_review_slot(str(slot), env))
    assert not slot.exists() and not lock.exists()


def test_the_native_adapters_tempdir_does_not_hide_the_hosts_tmpdir(tmp_path, monkeypatch):
    """launch_spec swaps tempfile.tempdir for its run directory; the pool choice must not follow it."""
    import tempfile

    from plangraph.spawner.host_ready_source import review_pool

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", "/srv/plangraph-fixture/tmp")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "state/launch/run"))
    primary = Path("/srv/plangraph-fixture/tmp/cargo-target")
    assert review_pool(primary) == tmp_path / "home/.cache/plangraph/cargo-target-pool"
    # A target outside every temp root keeps its own pool.
    elsewhere = Path("/srv/plangraph-fixture/cache/cargo-target")
    assert review_pool(elsewhere) == Path("/srv/plangraph-fixture/cache/cargo-target-pool")


def test_a_held_review_slot_is_not_dropped_and_keeps_its_lock(tmp_path):
    from plangraph.spawner.host_ready import _cargo_lease_for
    from plangraph.spawner.launch_spec import drop_review_slot

    slot = tmp_path / "cargo-target-pool" / "slot-review-job1"
    (slot / "target").mkdir(parents=True)
    held = _cargo_lease_for(slot)  # a seat (or hygiene) holds the slot
    drop_review_slot(str(slot))
    assert (slot / "target").exists() and Path(str(slot) + ".plangraph-lock").exists()
    held.close()
    drop_review_slot(str(slot))
    assert not slot.exists() and not Path(str(slot) + ".plangraph-lock").exists()


def test_a_symlinked_tmpdir_is_followed_for_a_target_that_does_not_exist_yet(tmp_path, monkeypatch):
    """Seat placement and hygiene (Rust under_roots) resolve existing ancestors the same way."""
    from plangraph.spawner.host_ready_source import review_pool

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(link))
    fallback = tmp_path / "home/.cache/plangraph/cargo-target-pool"
    assert review_pool(real / "absent/cargo-target") == fallback
    assert review_pool(link / "absent/cargo-target") == fallback
    monkeypatch.setenv("TMPDIR", str(real))
    assert review_pool(link / "absent/cargo-target") == fallback


def test_the_runners_cleanup_takes_a_review_slots_lock_file_and_leaves_other_dirs_plain(tmp_path):
    from plangraph.spawner.host_ready import _cargo_lease_for
    from plangraph.spawner.host_ready_source import remove_run_dir

    slot = tmp_path / "cargo-target-pool" / "slot-review-job9"
    slot.mkdir(parents=True)
    _cargo_lease_for(slot).close()  # what the seat's lease leaves behind
    other = tmp_path / "pg-ocdata-x"
    other.mkdir()
    remove_run_dir(str(slot))
    remove_run_dir(str(other))
    assert not slot.exists() and not Path(str(slot) + ".plangraph-lock").exists()
    assert not other.exists()


def test_a_review_seat_with_neither_target_nor_allocator_is_refused_not_built_under_tmp():
    from plangraph.planner.legs import EXPLICIT_LEGS, REVIEW_SONNET
    from plangraph.routing_support import claude_seat

    leg = next(leg for leg in EXPLICIT_LEGS if leg.id == REVIEW_SONNET)
    argv = claude_access_args("review")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(claude_seat, "sandbox_available", lambda _: True, raising=False)
        with pytest.raises(ValueError, match="slot allocator"):
            claude_seat.configure_review_tests(argv, {}, leg, "review", {}, "job1")
