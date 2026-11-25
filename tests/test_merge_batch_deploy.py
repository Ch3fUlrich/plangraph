"""tools/merge_batch.py go-req: the deploy blocks of the GO-REQ line.

HERMETIC (D-852), see test_merge_batch.py: a temp bare origin plus clone and a stub gh; PLANGRAPH_BIN is a stub
that answers `land-train deploy-plan` (or the real binary in one test), and the keeper is a patched call.

Mutations killed: a deploy plan that is missing from the GO-REQ or put after PLAN, units read from the worktree
instead of the reported head, a Deploy-Hosts declaration read for a unitless part or not read for a unit part,
a keeper answer of another item taken for the declaration, a hosts registry looked up outside XDG_CONFIG_HOME,
a launch failure that escapes as a traceback.
"""
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

from merge_batch_env import ROOT, env_fixture, train_cli  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")


@pytest.fixture
def env(tmp_path):
    yield from env_fixture(tmp_path)


FAKE_PLANGRAPH = """#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
if args[:2] != ["land-train", "deploy-plan"]:
    sys.exit(subprocess.call([os.environ["MAIN_STATE_STUB"], *args]))
flags = dict(zip(args[slice(2, None, 2)], args[slice(3, None, 2)]))
if "--derive-only" in flags:
    parts = os.environ.get("DEPLOY_PLAN_PARTS")
    print(open(parts).read() if parts and os.path.exists(parts) else "[]")
    sys.exit(0)
units_dir = flags["--probe-cmd"].split()[-1]
record = dict(argv=args, spans=json.load(open(flags["--spans"])), declared=json.load(open(flags["--declared"])),
              units={n: open(os.path.join(units_dir, n)).read() for n in os.listdir(units_dir)})
open(os.environ["DEPLOY_PLAN_CALL"], "w").write(json.dumps(record))
if os.path.exists(os.environ["DEPLOY_PLAN_FAIL"]):
    print("plan exploded", file=sys.stderr)
    sys.exit(3)
json.dump({"items": [{"key": "plangraph:LE-DEPLOY-BLOCKED-X"}]}, open(flags["--blockers-out"], "w"))
print(open(os.environ["DEPLOY_PLAN_OUT"]).read(), end="")
"""


def deploy_fixture(env, report, config_root=None):
    """A PLANGRAPH_BIN that answers `land-train deploy-plan` with ``report`` and records how it was called."""
    stub = env.tmp / "plangraph-fake"
    stub.write_text(FAKE_PLANGRAPH)
    stub.chmod(0o755)
    env.env.update(PLANGRAPH_BIN=str(stub), MAIN_STATE_STUB=str(env.stubs / "main-state"),
                   DEPLOY_PLAN_CALL=str(env.tmp / "call.json"), DEPLOY_PLAN_OUT=str(env.tmp / "report.txt"),
                   DEPLOY_PLAN_FAIL=str(env.tmp / "fail"), DEPLOY_PLAN_PARTS=str(env.tmp / "parts.json"))
    (env.tmp / "report.txt").write_text(report)
    registry = (config_root or env.home / ".config") / "plangraph" / "hosts.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{}")
    return registry


def test_go_req_carries_the_deploy_plan_of_the_train_parts(env):
    from test_merge_batch_land import go_ready

    registry = deploy_fixture(env, "deploy: unit.service@code\nREADY unit.service@code parts #1\n")
    st = go_ready(env)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert "; deploy: unit.service@code; READY unit.service@code parts #1; PLAN plangraph:BATCH" in result.stdout
    call = json.loads((env.tmp / "call.json").read_text())
    argv = call["argv"]
    flags = dict(zip(argv[slice(2, None, 2)], argv[slice(3, None, 2)]))
    assert flags["--registry"] == str(registry) and flags["--repo"] == st["worktree"]
    probe = flags["--probe-cmd"].split()
    assert probe[1:-1] == ["host-sync", "--install-dry-run", "--registry", str(registry), "--repo", st["worktree"],
                           "--units"]
    assert probe[-1].endswith("/deploy/systemd")
    # Each part is the range of its own commits: the merge's first parent to the part head.
    spans = {row["pr"]: row for row in call["spans"]}
    for rec in st["merged"]:
        first_parent_of_merge = env.git("rev-parse", f"{rec['merge']}^1", cwd=st["worktree"])
        assert spans[rec["pr"]] == {"pr": rec["pr"], "from": first_parent_of_merge, "head": rec["head"]}


def test_go_req_lists_the_blocker_item_of_a_failed_dry_run(env):
    from test_merge_batch_land import go_ready

    report = ("deploy: none\nNOT READY unit.service@code parts #1 blocker plangraph:LE-DEPLOY-BLOCKED-X\n"
              "MISSING env PG_PINS\nMISSING binary ~/.local/bin/tool\n")
    deploy_fixture(env, report)
    go_ready(env)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1
    assert ("; deploy: none; NOT READY unit.service@code parts #1 blocker plangraph:LE-DEPLOY-BLOCKED-X "
            "(MISSING env PG_PINS, MISSING binary ~/.local/bin/tool); PLAN") in result.stdout


def test_go_req_refuses_when_the_deploy_plan_cannot_be_made(env):
    from test_merge_batch_land import go_ready

    deploy_fixture(env, "deploy: none\n")
    (env.tmp / "fail").write_text("")
    go_ready(env)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 1
    assert "deploy-plan failed: plan exploded" in result.stderr


def test_go_req_says_so_when_there_is_no_hosts_registry(env):
    from test_merge_batch_land import go_ready

    go_ready(env)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert "; deploy: unchecked (no hosts registry); PLAN" in result.stdout


def test_go_req_runs_the_real_deploy_plan_for_a_unit_part_and_a_unitless_part(env, train_cli):
    """The chain end to end: the unit part's `Deploy:` host runs the real host install dry run."""
    from test_merge_batch_land import go_ready

    wrapper = env.tmp / "plangraph-real"
    wrapper.write_text(f"#!/bin/sh\n[ \"$1\" = main-state ] && exec {env.stubs / 'main-state'} \"$@\"\nexec {train_cli} \"$@\"\n")
    wrapper.chmod(0o755)
    env.env["PLANGRAPH_BIN"] = str(wrapper)
    registry = env.home / ".config" / "plangraph" / "hosts.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    example = json.loads((Path(__file__).resolve().parents[1] / "docs/schemas/examples/hosts.example.json").read_text())
    registry.write_text(json.dumps(example))
    unit = "[Service]\nEnvironmentFile=%h/.config/p/t.env\nExecStart=%h/.local/bin/train --pins ${PG_PINS}\n"
    st = go_ready(env, first_files={"deploy/systemd/train.service": unit}, first_trailer="\nDeploy: code", prs="1,2")
    assert [rec["pr"] for rec in st["merged"]] == [1, 2]  # PR 2 is the unitless part
    # An uncommitted edit of the unit in the worktree must not change what the reported head needs.
    (Path(st["worktree"]) / "deploy/systemd/train.service").write_text("[Service]\nExecStart=/bin/true\n")
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert ("; deploy: none; NOT READY train.service@code parts #1 blocker plangraph:LE-DEPLOY-BLOCKED-TRAIN-SERVICE-CODE "
            "(MISSING binary ~/.local/bin/train, MISSING env PG_PINS, MISSING file ~/.config/p/t.env); PLAN") in result.stdout
    # A host that has everything is READY, and the unitless part adds no family.
    bin_path = env.home / ".local/bin/train"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("")
    bin_path.chmod(0o755)
    (env.home / ".config/p").mkdir(parents=True)
    (env.home / ".config/p/t.env").write_text("PG_PINS=/etc/hostname\n")
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert "; deploy: train.service@code; READY train.service@code parts #1; PLAN" in result.stdout


def test_go_req_finds_the_hosts_registry_through_xdg_config_home(env):
    from test_merge_batch_land import go_ready

    xdg = env.tmp / "xdg"
    registry = deploy_fixture(env, "deploy: none\n", config_root=xdg)
    go_ready(env)
    # The keeper config moves with XDG_CONFIG_HOME too; the registry sits only under it.
    shutil.copy(env.home / ".config" / "plangraph" / "keeper.json", xdg / "plangraph" / "keeper.json")
    env.env["XDG_CONFIG_HOME"] = str(xdg)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert "; deploy: none; PLAN" in result.stdout and "unchecked" not in result.stdout
    call = json.loads((env.tmp / "call.json").read_text())["argv"]
    assert str(registry) in call


def test_go_req_passes_the_item_declared_hosts_and_the_unit_sources_of_the_head(env, monkeypatch):
    from test_merge_batch_land import go_ready

    sys.path.insert(0, str(ROOT / "tools"))
    import merge_batch_plan
    from merge_batch_util import active_train

    deploy_fixture(env, "deploy: none\n")
    (env.tmp / "parts.json").write_text(json.dumps([{"pr": 1, "families": ["train.service"],
                                                     "items": ["plangraph:ITEM-A"], "trailer_hosts": []}]))
    unit = "[Service]\nExecStart=%h/.local/bin/train\n"
    st = go_ready(env, first_files={"deploy/systemd/train.service": unit}, prs="1")
    # An uncommitted edit and an untracked unit are not part of the reported head.
    (Path(st["worktree"]) / "deploy/systemd/train.service").write_text("edited\n")
    (Path(st["worktree"]) / "deploy/systemd/extra.service").write_text("untracked\n")
    for key, value in env.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(merge_batch_plan, "keeper_landing_configured", lambda: True)
    monkeypatch.setattr(merge_batch_plan, "file_deploy_blockers", lambda path: None)
    item = {"key": "plangraph:ITEM-A", "description": "Why: x\nDeploy-Hosts: alpha, beta"}
    monkeypatch.setattr(merge_batch_plan, "keeper_cli", lambda *a: types.SimpleNamespace(
        returncode=0, stdout=json.dumps(item), stderr=""))
    monkeypatch.setattr(merge_batch_plan, "git", lambda *a, cwd=None, **k: types.SimpleNamespace(
        stdout=env.git(*a, cwd=cwd) + "\n"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert merge_batch_plan.deploy_report(st, active_train(st, st["head"])).startswith("deploy: none")
    call = json.loads((env.tmp / "call.json").read_text())
    assert call["declared"] == {"plangraph:ITEM-A": ["alpha", "beta"]}
    assert call["units"] == {"train.service": unit}


def test_a_unitless_part_needs_no_keeper_lookup_but_a_unit_part_does(env, monkeypatch):
    from test_merge_batch_land import go_ready

    sys.path.insert(0, str(ROOT / "tools"))
    import merge_batch_plan
    from merge_batch_util import active_train

    deploy_fixture(env, "deploy: none\n")
    st = go_ready(env)
    for key, value in env.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(merge_batch_plan, "keeper_landing_configured", lambda: True)
    monkeypatch.setattr(merge_batch_plan, "file_deploy_blockers", lambda path: None)
    monkeypatch.setattr(merge_batch_plan, "keeper_cli", lambda *a: types.SimpleNamespace(
        returncode=1, stdout="", stderr="keeper unreachable"))
    monkeypatch.setattr(merge_batch_plan, "git", lambda *a, cwd=None, **k: types.SimpleNamespace(
        stdout=env.git(*a, cwd=cwd) + "\n"))
    active = active_train(st, st["head"])
    parts = env.tmp / "parts.json"
    # No part ships a family: no host is asked about, so a down keeper does not matter.
    parts.write_text(json.dumps([{"pr": 1, "families": [], "items": ["plangraph:ITEM-A"], "trailer_hosts": []}]))
    assert merge_batch_plan.deploy_report(st, active).startswith("deploy: none")
    # One part ships a unit: its items need their declarations, and the lookup that fails refuses.
    parts.write_text(json.dumps([{"pr": 1, "families": ["a.service"], "items": ["plangraph:ITEM-A"],
                                  "trailer_hosts": []}]))
    with pytest.raises(merge_batch_plan.MergeBatchError, match="Deploy-Hosts of plangraph:ITEM-A"):
        merge_batch_plan.deploy_report(st, active)


def test_a_stale_plangraph_binary_is_a_go_req_error_not_a_traceback(env):
    from test_merge_batch_land import go_ready

    deploy_fixture(env, "deploy: none\n")
    env.env["PLANGRAPH_BIN"] = str(env.tmp / "gone" / "plangraph")
    go_ready(env)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 1
    assert "deploy-plan failed" in result.stderr and "Traceback" not in result.stderr
