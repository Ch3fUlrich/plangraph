"""Batch assembly applies the part-entry predicate to every part (LAND-PART-ENTRY-CHECKS)."""
import json
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


@pytest.mark.parametrize("path,runner_wide", [
    ("tests/conftest.py", True),
    ("tools/pgtest.py", True),
    ("rust-toolchain.toml", True),
    ("rust/worker-core/src/run/group_windows.rs", True),
    ("rust/worker-core/src/run/supervise.rs", True),
    ("rust/worker-core/src/stall/deadline.rs", True),
    ("src/plangraph/spawner/proc_kill.py", True),
    ("src/plangraph/planner/provider_stop.py", False),
])
def test_runner_wide_part_requires_full_ci_before_build(env, path, runner_wide):
    head = env.branch_pr(1, {path: "value = 1\n"})
    result = env.run("build", "--name", "runner-wide", "--prs", "1", "--force-set")
    if not runner_wide:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1, result.stderr
        assert f"PR #1: runner-wide part {head} needs green full CI" in result.stderr
        assert not env.state_file("runner-wide").exists()


@pytest.mark.parametrize("case", ["dispatch", "pull_request", "affected-only", "wrong-head",
                                 "wrong-tree", "failed-job"])
def test_runner_wide_receipt_proves_a_full_run_on_the_part_head(env, monkeypatch, case):
    from merge_batch_land import JOB_DISPLAY, SHARD_DISPLAY, render_body

    head = env.branch_pr(1, {"tests/conftest.py": "value = 1\n"})
    tree = env.git("rev-parse", f"{head}^{{tree}}")
    monkeypatch.chdir(env.repo)
    event = "pull_request" if case in ("pull_request", "affected-only", "wrong-tree") else "workflow_dispatch"
    run = {"id": 51, "event": event, "head_sha": head if case != "wrong-head" else "f" * 40,
           "head_branch": "lane/p1", "status": "completed", "conclusion": "success",
           "run_attempt": 2, "pull_requests": [{"number": 1}]}
    names = [*JOB_DISPLAY.values(), *(f"{name} (shard {i} of 3)" for name in SHARD_DISPLAY.values()
                                     for i in range(1, 4))]
    jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in names]
    if case == "failed-job":
        jobs[-1]["conclusion"] = "skipped"

    def api(_verb, endpoint):
        if "/workflows/" in endpoint:
            return {"workflow_runs": [run]}
        if "/statuses?" in endpoint:
            if case == "affected-only":
                return []
            tested = "e" * 40 if case == "wrong-tree" else tree
            return [{"context": "ci-full", "state": "success",
                     "description": f"run 51 attempt 2 tree {tested}"}]
        assert "/runs/51/attempts/2/jobs?" in endpoint
        return {"jobs": jobs, "total_count": len(jobs)}

    pr = {"head": head, "ref": "lane/p1", "title": "Part", "own_paths": ["tests/conftest.py"]}
    if case in ("dispatch", "pull_request"):
        receipts = build.runner_wide_receipts("o/p", {1: pr}, [1], api=api)
        assert receipts == {1: 51}
        env.git("checkout", "-q", "main")
        env.git("merge", "--no-ff", "-m", "Merge fixture", head)
        merge = env.git("rev-parse", "HEAD")
        state = {"name": "train", "base": "main", "merged": [
            {"pr": 1, "head": head, "merge": merge, "items": [], "resolved": False}],
            "prs": {"1": dict(pr, full_ci_run=receipts[1])}, "skipped": [], "worktree": env.repo}
        assert "Full CI run 51" in render_body(state)
    else:
        with pytest.raises(build.MergeBatchError, match=f"runner-wide part {head} needs green full CI"):
            build.runner_wide_receipts("o/p", {1: pr}, [1], api=api)


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
    ok = build.part_readiness_problems("o/p", {1: info()}, [1], attempts_for=lambda *_: [{"attempt": 1, "conclusion": "success"}])
    assert ok == []
    red = build.part_readiness_problems("o/p", {1: info(ci="red")}, [1], attempts_for=lambda *_: [])
    assert any("seconds-fast checks did not pass" in p for p in red) and all(p.startswith("PR #1: ") for p in red)


def test_a_rerun_that_turned_green_needs_the_flake_disposition_label():
    rows = lambda *_: [{"attempt": 1, "conclusion": "failure"}, {"attempt": 2, "conclusion": "success"}]  # noqa: E731
    bare = build.part_readiness_problems("o/p", {1: info()}, [1], attempts_for=rows)
    assert bare and "no flake disposition recorded" in bare[0]
    labelled = build.part_readiness_problems("o/p", {1: info(labels=[build.FLAKE_LABEL])}, [1], attempts_for=rows)
    assert labelled == []


def test_the_attempts_of_a_rerun_run_are_listed_oldest_first(monkeypatch):
    head = "d" * 40
    def api(*args):
        endpoint = args[1]
        if "/jobs" in endpoint:
            return {"jobs": [{"name": "ci-gate", "status": "completed", "conclusion": "success"}]}
        if endpoint.endswith("/attempts/1"):
            return {"status": "completed", "conclusion": "cancelled"}
        return {"workflow_runs": [
            {"id": 5, "head_sha": head, "status": "completed", "conclusion": "success", "run_attempt": 2,
             "pull_requests": [{"number": 1}]},
            {"id": 4, "head_sha": "e" * 40, "status": "completed", "conclusion": "failure",
             "pull_requests": [{"number": 1}]}]}

    monkeypatch.setattr(build, "gh_json", api)
    assert build.ci_attempts("o/p", 1, head) == [{"run_id": 5, "attempt": 1, "conclusion": "cancelled"},
                                                 {"run_id": 5, "attempt": 2, "conclusion": "success"}]


def test_unfetchable_open_pr_uses_its_advertised_local_commit(env, monkeypatch):
    head = env.branch_pr(9, {"outside.py": "a = 1\n"})
    env.git("push", "origin", ":refs/pull/9/head")
    monkeypatch.chdir(env.repo)
    monkeypatch.setattr(build, "gh_json", lambda *args: [
        {"number": 9, "headRefOid": head, "headRefName": "lane/p9", "baseRefName": "main"}])
    assert build.open_pr_heads("pe", "o/p", {})[9]["head"] == head


def test_unavailable_open_pr_refuses_entry(env, monkeypatch):
    monkeypatch.chdir(env.repo)
    monkeypatch.setattr(build, "gh_json", lambda *args: [
        {"number": 9, "headRefOid": "a" * 40, "headRefName": "lane/p9", "baseRefName": "main"}])
    with pytest.raises(build.MergeBatchError, match="open PR #9"):
        build.open_pr_heads("pe", "o/p", {})


def test_green_rollup_without_tests_run_cannot_admit_a_part(monkeypatch):
    monkeypatch.setattr(build, "gh_json", lambda *args: {"workflow_runs": []})
    assert "PR #1: no completed attempt on this head" in build.part_readiness_problems("o/p", {1: info()}, [1])


def test_green_workflow_without_a_successful_ci_gate_cannot_admit_a_part(monkeypatch):
    def api(*args):
        if "/jobs" in args[1]:
            return {"jobs": [{"name": "unrelated", "status": "completed", "conclusion": "success"}]}
        return {"workflow_runs": [{"id": 5, "head_sha": info()["head"], "status": "completed",
                                   "conclusion": "success", "pull_requests": [{"number": 1}]}]}
    monkeypatch.setattr(build, "gh_json", api)
    assert build.part_readiness_problems("o/p", {1: info()}, [1])


def test_newer_red_workflow_overrides_older_green_rerun():
    rows = [{"run_id": 5, "attempt": 1, "conclusion": "failure"},
            {"run_id": 5, "attempt": 2, "conclusion": "success"},
            {"run_id": 6, "attempt": 1, "conclusion": "failure"}]
    problems = build.part_readiness_problems("o/p", {1: info(labels=[build.FLAKE_LABEL])}, [1],
                                             attempts_for=lambda *args: rows)
    assert any("latest completed attempt" in p and "failure" in p for p in problems)


def test_a_green_part_with_changed_tests_needs_a_replay_receipt_bound_to_its_head(tmp_path, monkeypatch):
    """Green CI is not proof that the five-run replay ran: the receipt must exist for this exact head."""
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path))
    ok = lambda *_: [{"attempt": 1, "conclusion": "success"}]  # noqa: E731
    part = info(files=("tests/test_a.py",))
    missing = build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok)
    assert any("five-run replay" in p for p in missing)
    folder = tmp_path / "entry-replay" / part["head"]
    folder.mkdir(parents=True)
    other = dict(head="b" * 40, ok=True, runs=5, flaky=[], failed=[], logs={}, changed={"python": ["tests/test_a.py"], "rust": [], "files": ["tests/test_a.py"]})
    (folder / "replay.json").write_text(json.dumps(other))
    assert any("five-run replay" in p for p in build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok))
    (folder / "replay.json").write_text(json.dumps(other | {"head": part["head"]}))
    assert build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok) == []
    wrong_base = other | {"head": part["head"], "changed": {"python": [], "rust": [], "files": []}}   # zero changed tests: another comparison base
    (folder / "replay.json").write_text(json.dumps(wrong_base))
    assert any("another comparison base" in p for p in build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok))
    flaky = other | {"head": part["head"], "ok": False, "flaky": ["tests/test_a.py::t"],
                     "logs": {"tests/test_a.py::t": ["fail.log", "pass.log"]}}
    (folder / "replay.json").write_text(json.dumps(flaky))
    problems = build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok)
    assert "PR #1: FLAKY: tests/test_a.py::t (logs fail.log and pass.log)" in problems


def test_a_part_without_changed_tests_needs_no_replay_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path))
    ok = lambda *_: [{"attempt": 1, "conclusion": "success"}]  # noqa: E731
    assert build.part_readiness_problems("o/p", {1: info()}, [1], attempts_for=ok) == []


def test_readiness_compares_the_receipt_with_the_inventory_of_the_parts_own_diff(tmp_path, monkeypatch):
    import merge_batch_plan as plan
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path))
    ok = lambda *_: [{"attempt": 1, "conclusion": "success"}]  # noqa: E731
    part = info(files=("rust/pg-spawnd/src/lib.rs",))
    covered = {"python": [], "rust": [["rust/pg-spawnd/Cargo.toml", "kept"]], "browser": [], "files": list(part["files"])}
    folder = tmp_path / "entry-replay" / part["head"]
    folder.mkdir(parents=True)
    receipt = dict(head=part["head"], ok=True, runs=5, flaky=[], failed=[], logs={})
    (folder / "replay.json").write_text(json.dumps(receipt | {"changed": covered}))
    (folder / "replay-windows.json").write_text(json.dumps(receipt | {"environment": "windows"}))   # a Rust test also needs the Windows run
    seen = []
    monkeypatch.setattr(plan, "changed_tests", lambda root, base, head: seen.append((base, head)) or
                        {"python": [], "rust": covered["rust"] + [["rust/pg-spawnd/Cargo.toml", "other"]], "browser": [], "files": covered["files"]})
    problems = build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok, bases={1: "origin/main"})
    assert any("another comparison base" in p for p in problems) and seen == [("origin/main", part["head"])]
    monkeypatch.setattr(plan, "changed_tests", lambda *_a: covered)
    assert build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok, bases={1: "origin/main"}) == []
    (folder / "replay-windows.json").unlink()
    assert any("windows environment" in p for p in build.part_readiness_problems("o/p", {1: part}, [1], attempts_for=ok, bases={1: "origin/main"}))


def test_a_stacked_child_is_compared_with_its_own_pull_request_base_not_with_main(tmp_path, monkeypatch):
    """The child's receipt covers its own tests: readiness recomputes the inventory against the parent's head."""
    import subprocess
    root = tmp_path / "r"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *a],  # noqa: E731
                                    check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q", "-b", "main")
    (root / "tests").mkdir()
    (root / "tests/test_base.py").write_text("def test_b():\n    pass\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    main = run("rev-parse", "HEAD")
    (root / "tests/test_parent.py").write_text("def test_p():\n    pass\n")
    run("add", "-A")
    run("commit", "-qm", "parent")
    parent = run("rev-parse", "HEAD")
    (root / "tests/test_child.py").write_text("def test_c():\n    pass\n")
    run("add", "-A")
    run("commit", "-qm", "child")
    child = run("rev-parse", "HEAD")
    monkeypatch.chdir(root)
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    folder = tmp_path / "telemetry/entry-replay" / child
    folder.mkdir(parents=True)
    covered = {"python": ["tests/test_child.py"], "rust": [], "browser": [], "files": ["tests/test_child.py"]}   # what GitHub shows for the child
    (folder / "replay.json").write_text(json.dumps(dict(head=child, ok=True, runs=5, flaky=[], failed=[], logs={}, changed=covered)))
    part = info(head=child, files=("tests/test_child.py",))
    ok = lambda *_: [{"attempt": 1, "conclusion": "success"}]  # noqa: E731
    assert build.part_readiness_problems("o/p", {2: part}, [2], attempts_for=ok, bases={2: parent}) == []
    wrong = build.part_readiness_problems("o/p", {2: part}, [2], attempts_for=ok, bases={2: main})   # the parent's tests would be included
    assert any("another comparison base" in p for p in wrong)
