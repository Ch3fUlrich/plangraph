"""tools/merge_batch.py build: crash recovery, the base and plan rules, the union warning.

HERMETIC (D-852), see test_merge_batch.py. A crash is a real one: the tool runs with a `git` on its PATH
that kills the tool right after its n-th `git <subcommand>` (merge, worktree), so the state file is
what a build killed by the OOM killer leaves.

Mutations killed: a state saved after the worktree or the merge it describes, a merge made before the
crash recorded as "already in base" and lost, an open conflict lost, a half-made build that abort cannot
find, an orphan that abort or status misses (or a foreign batch/* branch they take), a PR stacked on a PR
outside the batch built against that PR's branch, a build that skips the plan rules or a --force-set
that is not logged, a rename out of a HIGH path built with standard PRs, a union merge that changed an
old line without a warning.
"""
import json
import sys

import pytest

from merge_batch_env import conflicting, env_fixture, first_parent, two

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")

LEDGER = "# Changelog\n\n## Unreleased\n\n- old entry\n"


@pytest.fixture
def env(tmp_path):
    yield from env_fixture(tmp_path)


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
        kw1 = {"lines": 2000}
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
