"""tools/merge_batch.py build: crash recovery, the base and plan rules, the union warning.

HERMETIC (D-852), see test_merge_batch.py. A crash is a real one: the tool runs with a `git` on its PATH
that kills the tool right after its n-th `git <subcommand>` (merge, worktree), so the state file is
what a build killed by the OOM killer leaves. The same wrapper can also make that git fail after its
side effect, or refuse to run (see Env.run).

Mutations killed: a state saved after the worktree or the merge it describes, a merge made before the
crash recorded as "already in base" and lost, an open conflict lost, a half-made build that abort cannot
find, a failed `worktree add` whose state is dropped so abort cannot find its leftovers, a merge made
over an unfinished one after a failed `merge --abort` cleared the marker, an orphan that abort or status
misses (or a foreign batch/* branch they take), a PR stacked on a PR outside the batch built against
that PR's branch, a build that skips the plan rules or a --force-set that is not logged, a rename out of
a HIGH path built with standard PRs, a union merge that changed an old line without a warning.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

from merge_batch_env import conflicting, env_fixture, first_parent, two

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")

LEDGER = "# Changelog\n\n## Unreleased\n\n- old entry\n"


@pytest.fixture
def env(tmp_path):
    yield from env_fixture(tmp_path)


@pytest.mark.parametrize("response", [
    "[]", '[{"name": "batch-ready"}]', "{}", "null", '"needs-update"',
    '[null, {"name": "needs-update"}]', '[{"name": "needs-update"}, {}]', "not JSON",
])
def test_skip_conflicts_checks_returned_label_list(env, response):
    conflicting(env)
    env.state["labels"].append("needs-update")
    env.state["label_response"] = response
    env.save()
    res = env.run("build", "--name", "label-response", "--prs", "1,2,3", "--skip-conflicts")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "could not add the needs-update label to PR #2" in res.stderr
    state = json.loads(env.state_file("label-response").read_text())
    assert state["skipped"][0]["labeled"] is False
    assert len([c for c in env.calls("api") if any("/labels" in part for part in c)]) == 1 \
        and not env.calls("label", "create")
    assert not env.calls("pr", "edit")


def wt_of(env, name):
    return env.tmp / "repo-worktrees" / f"batch-{name}"


def read(env, name):
    return json.loads(env.state_file(name).read_text())


def three(env):
    env.branch_pr(1, {"a.txt": "a\n"}, item="ITEM-A")
    env.branch_pr(2, {"b.txt": "b\n"}, item="ITEM-B")
    env.branch_pr(3, {"c.txt": "c\n"})


# ----------------------------------------------------------------------------------- crashes
def test_a_crash_after_a_merge_is_recovered_by_continue(env):
    three(env)
    res = env.run("build", "--name", "k1", "--prs", "1,2,3", crash=("merge", 2))
    assert res.returncode == -9  # killed right after the merge commit of PR 2, before it was recorded
    st = read(env, "k1")
    assert (st["status"], st["merging"]["pr"], st["pending"], [m["pr"] for m in st["merged"]]) == (
        "building", 2, [2, 3], [1])  # the state was saved before the merge
    assert first_parent(env, "batch/k1", wt_of(env, "k1"))[0] == "Merge PR #2: change 2"
    assert "died while merging PR #2" in env.run("status").stdout
    res = env.run("continue", "--name", "k1")
    assert res.returncode == 0 and "recovered the merge of PR #2" in res.stderr, res.stderr
    st = read(env, "k1")
    assert st["status"] == "built" and st["merging"] is None and st["skipped"] == []  # not "already in base"
    assert [(m["pr"], m["items"]) for m in st["merged"]] == [
        (1, ["plangraph:ITEM-A"]), (2, ["plangraph:ITEM-B"]), (3, [])]
    assert [m["recovered"] for m in st["merged"]] == [False, True, False]
    assert first_parent(env, "batch/k1", wt_of(env, "k1"))[:3] == [
        "Merge PR #3: change 3", "Merge PR #2: change 2", "Merge PR #1: change 1"]
    assert env.run("push", "--name", "k1").stdout == "100\n"
    body = env.load()["prs"]["100"]["body"]
    assert "| #2 | plangraph:ITEM-B |" in body and "merge found after an interrupted build" in body


def test_a_crash_inside_a_conflicting_merge_is_picked_up_by_continue(env):
    conflicting(env)
    wt = env.tmp / "wt"
    res = env.run("build", "--name", "k3", "--prs", "1,2,3", "--worktree", str(wt), crash=("merge", 2))
    assert res.returncode == -9 and read(env, "k3")["merging"]["pr"] == 2  # the conflict never reached the state
    res = env.run("continue", "--name", "k3")
    assert res.returncode == 3 and "CONFLICT PR #2" in res.stdout and "  shared.txt" in res.stdout
    st = read(env, "k3")
    assert (st["status"], st["merging"], st["pending"], st["current"]["files"]) == ("conflict", None, [3], ["shared.txt"])
    (wt / "shared.txt").write_text(two("line 3 both"))
    env.git("add", "shared.txt", cwd=wt)
    env.git("commit", "-q", "--no-edit", cwd=wt)
    assert env.run("continue", "--name", "k3").returncode == 0
    st = read(env, "k3")
    assert st["status"] == "built" and [m["pr"] for m in st["merged"]] == [1, 2, 3] and st["merged"][1]["resolved"]


def test_a_crash_while_the_worktree_is_made_leaves_a_state_abort_cleans(env):
    env.branch_pr(1, {"a.txt": "a\n"})
    res = env.run("build", "--name", "k2", "--prs", "1", crash=("worktree", 1))
    assert res.returncode == -9
    assert read(env, "k2")["status"] == "creating"  # saved before the worktree was made
    wt = wt_of(env, "k2")
    assert wt.exists() and env.git("branch", "--list", "batch/k2")
    assert "died while its worktree was made" in env.run("status").stdout
    res = env.run("continue", "--name", "k2")
    assert res.returncode == 1 and "abort it" in res.stderr
    assert env.run("abort", "--name", "k2").returncode == 0
    assert not wt.exists() and not env.state_file("k2").exists() and env.git("branch", "--list", "batch/k2") == ""
    assert env.git("for-each-ref", "refs/merge-batch") == ""
    assert env.run("build", "--name", "k2", "--prs", "1").returncode == 0  # the name is free again


def test_a_worktree_add_that_fails_after_it_made_the_branch_is_left_for_abort(env):
    """A `worktree add` that fails after its side effects (branch and directory made): the state stays
    (status creating) so status and abort find the leftovers, the error names abort, and abort removes
    exactly branch, worktree, PR-head refs and state (I9)."""
    env.branch_pr(1, {"a.txt": "a\n"})
    res = env.run("build", "--name", "w9", "--prs", "1", crash=("worktree", 1, "fail"))
    assert res.returncode == 1 and "abort --name w9" in res.stderr, res.stderr
    wt = wt_of(env, "w9")
    assert read(env, "w9")["status"] == "creating"  # kept: status and abort can find what the failed add left
    assert wt.exists() and env.git("branch", "--list", "batch/w9")
    out = env.run("status").stdout
    assert "w9  creating" in out and "abort --name w9" in out
    assert env.run("abort", "--name", "w9").returncode == 0
    assert not wt.exists() and not env.state_file("w9").exists()
    assert env.git("branch", "--list", "batch/w9") == "" and env.git("for-each-ref", "refs/merge-batch") == ""
    assert env.run("build", "--name", "w9", "--prs", "1").returncode == 0  # the name is free again


def test_a_merge_abort_that_fails_keeps_the_marker_and_the_merge_state(env):
    """Recovery undoes a merge that was cut off before its commit; when that undo fails nothing starts a
    new merge over the unfinished one (I10): the marker and MERGE_HEAD stay for the next continue."""
    head = env.branch_pr(1, {"a.txt": "a\n"})
    env.branch_pr(2, {"b.txt": "b\n"})
    assert env.run("build", "--name", "m1", "--prs", "1,2", crash=("worktree", 1)).returncode == -9
    wt = wt_of(env, "m1")
    env.git("merge", "--no-ff", "--no-commit", head, cwd=wt)  # a run died right here: no commit yet
    st = read(env, "m1")
    st.update(status="building", merging={"pr": 1, "head": head})
    env.state_file("m1").write_text(json.dumps(st))
    res = env.run("continue", "--name", "m1", crash=("merge", 1, "refuse"))
    assert res.returncode == 1 and "git merge --abort failed" in res.stderr, res.stderr
    assert read(env, "m1")["merging"] == {"pr": 1, "head": head}  # the marker stays saved
    assert Path(env.git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD", cwd=wt)).exists()
    assert env.git("rev-list", "--merges", "--count", "batch/m1") == "0"  # no new merge commit was made
    res = env.run("continue", "--name", "m1")  # with a working git the recovery goes on as before
    assert res.returncode == 0, res.stderr
    assert [m["pr"] for m in read(env, "m1")["merged"]] == [1, 2]


def test_abort_drops_a_build_that_died_right_after_a_merge(env):
    three(env)
    assert env.run("build", "--name", "k4", "--prs", "1,2,3", crash=("merge", 2)).returncode == -9
    assert read(env, "k4")["merging"]["pr"] == 2 and env.git("branch", "--list", "batch/k4")
    res = env.run("abort", "--name", "k4")
    assert res.returncode == 0, res.stderr
    assert not wt_of(env, "k4").exists() and not env.state_file("k4").exists()
    assert env.git("branch", "--list", "batch/k4") == "" and env.git("for-each-ref", "refs/merge-batch") == ""


def test_abort_cleans_a_batch_whose_worktree_directory_was_removed_by_hand(env):
    env.branch_pr(1, {"a.txt": "a\n"})
    assert env.run("build", "--name", "k8", "--prs", "1").returncode == 0
    shutil.rmtree(wt_of(env, "k8"))  # the admin entry stays, so `worktree list` still names it
    res = env.run("abort", "--name", "k8")
    assert res.returncode == 0, res.stderr
    assert not env.state_file("k8").exists() and env.git("branch", "--list", "batch/k8") == ""
    assert env.git("for-each-ref", "refs/merge-batch") == ""


def test_status_and_abort_find_an_orphan_by_branch_name(env):
    env.branch_pr(1, {"a.txt": "a\n"})
    assert env.run("build", "--name", "o1", "--prs", "1").returncode == 0
    env.state_file("o1").unlink()  # what a build that died before its first save left: refs, branch, worktree
    wt = wt_of(env, "o1")
    out = env.run("status").stdout
    assert "o1  orphan" in out and "abort --name o1" in out and str(wt) in out
    assert json.loads(env.run("status", "--json").stdout)[0]["status"] == "orphan"
    other = env.tmp / "other"  # a batch/* branch some other tool made: no PR-head refs, so not ours
    env.git("worktree", "add", "-q", "-b", "batch/other", str(other), "origin/main")
    assert "other" not in env.run("status").stdout
    res = env.run("abort", "--name", "other")
    assert res.returncode == 1 and "no batch 'other' in progress" in res.stderr and other.exists()
    res = env.run("abort", "--name", "o1", cwd=wt)  # from inside the worktree that goes away
    assert res.returncode == 0, res.stderr
    assert not wt.exists() and env.git("branch", "--list", "batch/o1") == "" and env.git("for-each-ref", "refs/merge-batch") == ""
    assert env.git("branch", "--list", "batch/other") != "" and other.exists()


# ------------------------------------------------------------------------------ base and rules
def test_build_refuses_a_pr_stacked_on_a_pr_outside_the_batch(env):
    env.branch_pr(4, {"a.txt": "a\n"}, ref="lane/s4")
    env.branch_pr(5, {"b.txt": "b\n"}, base="lane/s4", ref="lane/s5")
    env.branch_pr(6, {"c.txt": "c\n"}, base="lane/s4", ref="lane/s6")
    for prs in ("5", "5,6"):  # every root targets lane/s4, but main lacks PR 4: MERGED would be a lie
        res = env.run("build", "--name", "s1", "--prs", prs)
        assert res.returncode == 1 and "default branch 'main'" in res.stderr and "#5 -> 'lane/s4'" in res.stderr
        assert not env.state_file("s1").exists() and not wt_of(env, "s1").exists()
        assert env.git("branch", "--list", "batch/s1") == "" and env.git("for-each-ref", "refs/merge-batch") == ""
    ok = env.run("build", "--name", "s2", "--prs", "4,5,6")  # with PR 4 in the batch they stack on a member
    assert ok.returncode == 0, ok.stderr
    assert read(env, "s2")["base"] == "main"


def test_build_refuses_a_head_the_remote_does_not_serve_and_leaves_no_refs(env):
    env.branch_pr(1, {"a.txt": "a\n"})
    env.state["prs"]["1"]["headRefOid"] = "f" * 40  # GitHub shows another head than the one origin serves
    env.save()
    res = env.run("build", "--name", "m1", "--prs", "1")
    assert res.returncode == 1 and "run build again" in res.stderr
    assert not env.state_file("m1").exists() and env.git("for-each-ref", "refs/merge-batch") == ""
    assert env.run("status").stdout == "no batch in progress\n"  # no orphan listed either


@pytest.mark.parametrize("case, prs, message", [
    ("red_ci", "1,2", "#2 ci red"), ("needs_update", "1,2", "#2 label needs-update"),
    ("high_with_standard", "1,2", "reviewed apart"), ("max_prs", "1,2", "cuts them into 2 batches"),
    ("max_lines", "1", "over the caps")])
def test_build_applies_the_plan_rules_unless_forced(env, case, prs, message):
    files, kw1, kw2, extra = {"a.txt": "a\n"}, {}, {}, []
    if case == "red_ci":
        kw2 = {"ci": "red"}
    elif case == "needs_update":
        kw2 = {"labels": ("batch-ready", "needs-update")}
    elif case == "high_with_standard":
        files = {"src/plangraph/keeper/store.py": "x = 1\n"}
    elif case == "max_prs":
        extra = ["--max-prs", "1"]
    else:
        files = {"a.txt": "a\n" * 2000}
    env.branch_pr(1, files, **kw1)
    env.branch_pr(2, {"b.txt": "b\n"}, **kw2)
    res = env.run("build", "--name", "r1", "--prs", prs, *extra)
    assert res.returncode == 1 and message in res.stderr and "--force-set" in res.stderr, res.stderr
    assert not env.state_file("r1").exists() and not wt_of(env, "r1").exists()
    assert env.git("branch", "--list", "batch/r1") == "" and env.git("for-each-ref", "refs/merge-batch") == ""
    forced = env.run("build", "--name", "r2", "--prs", prs, "--force-set", *extra)  # the override is logged
    assert forced.returncode == 0 and "FORCE-SET" in forced.stderr and message in forced.stderr, forced.stderr
    st = read(env, "r2")
    assert st["status"] == "built" and message in " ".join(st["forced"])
    assert env.run("push", "--name", "r2").stdout == "100\n"
    assert "Built with --force-set" in env.load()["prs"]["100"]["body"] and message in env.load()["prs"]["100"]["body"]


def test_build_tiers_a_rename_by_its_old_path(env):
    keeper = "src/plangraph/keeper/store.py"
    env.seed({keeper: "x = 1\n"})
    env.branch_pr(1, {}, moves={keeper: "docs/store.py"})  # gh lists only docs/store.py
    env.branch_pr(2, {"b.txt": "b\n"})
    res = env.run("build", "--name", "h1", "--prs", "1,2")
    assert res.returncode == 1 and "HIGH #1" in res.stderr and "reviewed apart" in res.stderr
    assert env.run("build", "--name", "h2", "--prs", "1").returncode == 0  # a HIGH PR alone is a fine batch


# ------------------------------------------------------------------------------- union warning
@pytest.mark.parametrize("how", ["gitattributes", "union_option"])
def test_build_warns_when_a_union_merge_touched_an_old_line(env, how):
    """PR 1 rewords an entry, PR 2 adds one beside it: the union keeps the old text too. The warning names the PR
    that changed an old line; a PR that only adds lines, or changes a path that is not union merged, gets none."""
    env.seed({"CHANGELOG.md": LEDGER, **({".gitattributes": "CHANGELOG.md merge=union\n"} if how == "gitattributes" else {})})
    env.branch_pr(1, {"CHANGELOG.md": LEDGER.replace("- old entry", "- reworded entry")})
    env.branch_pr(2, {"CHANGELOG.md": LEDGER + "- two\n"})
    env.branch_pr(3, {"shared.txt": two("line 3 changed")})
    res = env.run("build", "--name", "w1", "--prs", "1,2,3", *(["--union", "CHANGELOG.md"] if how == "union_option" else []))
    assert res.returncode == 0, res.stdout + res.stderr
    text = (wt_of(env, "w1") / "CHANGELOG.md").read_text()
    assert "- reworded entry\n" in text and "- old entry\n" in text and "- two\n" in text  # the union kept both
    assert "warning: PR #1: CHANGELOG.md: 1 pre-existing line(s) changed or removed" in res.stderr
    assert "PR #2" not in res.stderr and "PR #3" not in res.stderr
    assert [len(m["warnings"]) for m in read(env, "w1")["merged"]] == [1, 0, 0]
    assert env.run("push", "--name", "w1").stdout == "100\n"
    body = env.load()["prs"]["100"]["body"]
    assert "Check these" in body and "- PR #1: CHANGELOG.md: 1 pre-existing line(s)" in body and "- PR #2" not in body


@pytest.mark.parametrize("legacy", [False, True])
def test_batch_changelog_and_checkpoint_appends_merge_by_default(env, legacy):
    """New lanes use unique records; historical same-place ledger edits retain both entries."""
    attrs = (Path(__file__).resolve().parents[1] / ".gitattributes").read_text()
    env.seed({".gitattributes": attrs, "CHANGELOG.md": LEDGER,
              "docs/checkpoints/README.md": "Directory is the index.\n"})
    for n in (1, 2):
        files = {f"changelog.d/ITEM-{n}.md": f"- change {n}\n",
                 f"docs/checkpoints/ITEM-{n}.md": f"Checkpoint {n}: done\n"}
        if legacy:
            files["CHANGELOG.md"] = LEDGER.replace("- old entry", f"- change {n}\n- old entry")
        env.branch_pr(n, files, item=f"ITEM-{n}")
    result = env.run("build", "--name", "docs", "--prs", "1,2")
    assert result.returncode == 0, result.stdout + result.stderr
    st = read(env, "docs")
    assert "CHANGELOG.md" in st["union"]
    wt = wt_of(env, "docs")
    for n in (1, 2):
        assert (wt / f"changelog.d/ITEM-{n}.md").read_text() == f"- change {n}\n"
        assert (wt / f"docs/checkpoints/ITEM-{n}.md").read_text() == f"Checkpoint {n}: done\n"
        if legacy:
            assert f"- change {n}\n" in (wt / "CHANGELOG.md").read_text()
    assert not any(m["warnings"] for m in st["merged"])


@pytest.mark.parametrize("ledger", ["release ledger.md", "docs/#notes.md"])
def test_union_defaults_come_from_fetched_base_not_callers_worktree(env, ledger):
    pattern = f'"{ledger}"' if " " in ledger else ledger
    env.seed({".gitattributes": f"{pattern} merge=union\n", ledger: "- old\n"})
    for n in (1, 2):
        env.branch_pr(n, {ledger: f"- change {n}\n- old\n"})
    (env.repo / ".gitattributes").write_text("# uncommitted caller edit must not erase policy\n")
    result = env.run("build", "--name", "policy", "--prs", "1,2", "--union", "extra.txt")
    assert result.returncode == 0, result.stderr
    assert read(env, "policy")["union"] == [pattern, 'extra.txt']
    assert (wt_of(env, "policy") / ledger).read_text().splitlines() == [
        "- change 1", "- change 2", "- old"]


@pytest.mark.parametrize("hook_rc", [0, 1])
def test_push_waits_for_slow_hook_and_records_pr(env, tools_importable, monkeypatch, capfd, hook_rc):
    import argparse
    import merge_batch_land
    import merge_batch_util

    env.branch_pr(1, {"rust/demo/src/lib.rs": "pub fn demo() {}\n"})
    assert env.run("build", "--name", "slow", "--prs", "1").returncode == 0
    hook = env.repo / ".git/hooks/pre-push"
    hook.write_text("#!/usr/bin/env python3\nimport time\n"
                    "print('slow Rust check', flush=True)\ntime.sleep(0.2)\n"
                    f"raise SystemExit({hook_rc})\n")
    hook.chmod(0o755)
    original = merge_batch_util.run

    def short_default(argv, **kwargs):
        if argv[:2] == ["git", "push"]:
            kwargs.setdefault("timeout", 0.05)  # scale the old 600s deadline down
        return original(argv, **kwargs)

    monkeypatch.setattr(merge_batch_util, "run", short_default)
    monkeypatch.chdir(env.repo)
    for key, value in env.env.items():
        monkeypatch.setenv(key, value)
    if hook_rc:
        with pytest.raises(merge_batch_util.MergeBatchError, match="failed"):
            merge_batch_land.cmd_push(argparse.Namespace(name="slow", ci_run=None))
        assert json.loads(env.state_file("slow").read_text())["status"] == "built"
        assert not env.calls("pr", "create")
        assert not env.git("for-each-ref", "refs/heads/batch/slow", cwd=env.origin)
        assert "slow Rust check" in capfd.readouterr().err
        return
    assert merge_batch_land.cmd_push(argparse.Namespace(name="slow", ci_run=None)) == 0
    state = json.loads(env.state_file("slow").read_text())
    assert (state["status"], state["pr"]) == ("pushed", 100)
    assert env.git("rev-parse", "refs/heads/batch/slow", cwd=env.origin) == state["head"]
    assert "slow Rust check" in capfd.readouterr().err


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-tree liveness check")
@pytest.mark.parametrize("nested_preflight", [False, True])
def test_push_timeout_kills_slow_hook_tree(env, tools_importable, monkeypatch, nested_preflight):
    import os
    import signal
    import merge_batch_util
    from plangraph.spawner.proc_kill import alive

    env.branch_pr(1, {"rust/demo/src/lib.rs": "pub fn demo() {}\n"})
    hook = env.repo / ".git/hooks/pre-push"
    hook.write_text("#!/usr/bin/env python3\nimport os, signal\n"
                    "child = os.fork()\n"
                    f"with open({str(env.tmp / 'hook-pids')!r} + str(os.getpid()), 'w') as f: f.write(str(os.getpid()))\n"
                    f"with open({str(env.tmp / 'hook-ready')!r}, 'wb', buffering=0) as f: f.write(b'x')\n"
                    "signal.pause()\n")
    if nested_preflight:
        payload = hook.read_text()
        hook.write_text("#!/usr/bin/env python3\nimport sys, subprocess\n"
                        f"sys.path.insert(0, {tools_importable!r})\nimport preflight\n"
                        f"preflight.run_test_process([sys.executable, '-c', {payload!r}], "
                        "stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)\n")
    hook.chmod(0o755)
    for key, value in env.env.items():
        monkeypatch.setenv(key, value)
    try:
        with env.hook_readiness(monkeypatch), pytest.raises(merge_batch_util.MergeBatchError, match="did not finish"):
            merge_batch_util.run(["git", "push", "origin", "lane/p1:refs/heads/timeout"],
                                 cwd=env.repo, timeout=0.3)
        records = list(env.tmp.glob("hook-pids*"))
        assert len(records) == 2, "hook and its child must both have started"
        for record in records:
            pid = int(record.read_text())
            assert not alive(pid, None), f"hook process {pid} survived timeout"
        assert not env.git("for-each-ref", "refs/heads/timeout", cwd=env.origin)
    finally:
        for record in env.tmp.glob("hook-pids*"):
            try:
                os.kill(int(record.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
