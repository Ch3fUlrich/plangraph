"""Batch assembly applies the part-entry predicate to every part (LAND-PART-ENTRY-CHECKS)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from merge_batch_env import env_fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import merge_batch_build as build  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")


@pytest.fixture
def env(tmp_path):
    yield from env_fixture(tmp_path)


def recording(code=0, out=""):
    calls = []

    def runner(argv):
        calls.append(argv)
        return code, out, ""

    return runner, calls


def test_each_part_is_checked_against_the_base_its_parent_and_the_heads_outside_its_chain():
    runner, calls = recording()
    heads = {1: "a" * 40, 2: "b" * 40, 3: "c" * 40, 4: "d" * 40}
    parent = {2: 1}                                   # 2 is stacked on 1; 3 and 4 stand alone
    assert build.part_entry_problems("main", [1, 2, 3, 4], heads, parent, runner) == []
    by_head = {c[2]: c for c in calls}
    assert by_head[heads[1]] == ["--part-entry", "origin/main", heads[1], "-", heads[3], heads[4]]
    assert by_head[heads[2]] == ["--part-entry", "origin/main", heads[2], heads[1], heads[3], heads[4]]
    assert by_head[heads[3]] == ["--part-entry", "origin/main", heads[3], "-", heads[1], heads[2], heads[4]]


def test_a_refusal_is_reported_per_part_with_its_reason():
    runner, _ = recording(1, "refused imported_history: merge abc takes def\n")
    problems = build.part_entry_problems("main", [7], {7: "e" * 40}, {}, runner)
    assert problems == ["PR #7: refused imported_history: merge abc takes def"]


def test_build_refuses_a_part_the_predicate_refuses_and_force_set_logs_it(env, tmp_path):
    env.branch_pr(1, {"app/a.py": "a = 1\n"})
    refusing = tmp_path / "refuse-policy"
    refusing.write_text("#!/bin/sh\necho 'refused shared_history: shares commits'\nexit 1\n")
    refusing.chmod(0o755)
    env.env["PG_POLICY_BIN"] = str(refusing)
    refused = env.run("build", "--name", "pe", "--prs", "1")
    assert refused.returncode != 0
    assert "part entry refused" in refused.stderr and "shared_history" in refused.stderr
    assert not env.calls("push")
    forced = env.run("build", "--name", "pe2", "--prs", "1", "--force-set")
    assert forced.returncode == 0, forced.stderr
    assert "FORCE-SET: building despite part entry" in forced.stderr


def test_open_pull_requests_outside_the_batch_are_compared_too():
    runner, calls = recording()
    heads = {1: "a" * 40}
    opens = {9: {"head": "f" * 40, "ref": "other", "base": "main"}}
    build.part_entry_problems("main", [1], heads, {}, runner, opens=opens, refs={1: "mine"})
    assert calls == [["--part-entry", "origin/main", heads[1], "-", "f" * 40]]


def test_a_part_stacked_on_an_open_pull_request_outside_the_batch_names_it_as_its_parent():
    runner, calls = recording()
    heads = {2: "b" * 40}
    opens = {1: {"head": "a" * 40, "ref": "parent-branch", "base": "main"},
             3: {"head": "c" * 40, "ref": "alone", "base": "main"}}
    build.part_entry_problems("main", [2], heads, {}, runner, opens={**opens, 2: {"head": heads[2], "ref": "child", "base": "parent-branch"}},
                              refs={2: "child"})
    assert calls == [["--part-entry", "origin/main", heads[2], "a" * 40, "c" * 40]]


def info(head="a" * 40, ci="green", files=("src/plangraph/planner/provider_stop.py",), labels=()):
    return {"head": head, "ci": ci, "files": list(files), "truncated": False, "labels": list(labels)}


def test_a_green_part_is_ready_and_a_part_without_green_ci_is_not():
    ok = build.part_readiness_problems("o/p", {1: info()}, [1], attempts_for=lambda r, h: [])
    assert ok == []
    red = build.part_readiness_problems("o/p", {1: info(ci="red")}, [1], attempts_for=lambda r, h: [])
    assert any("seconds-fast checks did not pass" in p for p in red) and all(p.startswith("PR #1: ") for p in red)


def test_a_rerun_that_turned_green_needs_the_flake_disposition_label():
    rows = lambda r, h: [{"attempt": 1, "conclusion": "failure"}, {"attempt": 2, "conclusion": "success"}]  # noqa: E731
    bare = build.part_readiness_problems("o/p", {1: info()}, [1], attempts_for=rows)
    assert bare and "no flake disposition recorded" in bare[0]
    labelled = build.part_readiness_problems("o/p", {1: info(labels=[build.FLAKE_LABEL])}, [1], attempts_for=rows)
    assert labelled == []


def test_the_attempts_of_a_rerun_run_are_listed_oldest_first(monkeypatch):
    head = "d" * 40
    monkeypatch.setattr(build, "gh_json", lambda *a, **k: {"workflow_runs": [
        {"id": 5, "head_sha": head, "status": "completed", "conclusion": "success", "run_attempt": 2},
        {"id": 4, "head_sha": "e" * 40, "status": "completed", "conclusion": "failure", "run_attempt": 1}]})
    assert build.ci_attempts("o/p", head) == [{"attempt": 1, "conclusion": "failure"},
                                              {"attempt": 2, "conclusion": "success"}]
