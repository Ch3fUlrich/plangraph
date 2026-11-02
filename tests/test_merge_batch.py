"""tools/merge_batch.py: plan, build, continue, push, land, status and abort.

HERMETIC (D-852): every repo is a temp bare origin plus clone, `gh` is a stub on a PATH that
holds only git, sh, env and python3 (it serves canned JSON and records its calls), and the tool
runs as a subprocess with a minimal env. `test_guard_real_gh_unreachable` fails if a real gh
could run; the `env` fixture fails any test whose tool called a gh command the stub lacks.

Mutations killed: a conflict merged blind or left half merged, a skipped PR not labelled, a --union
path or a missing .gitattributes still conflicting, a draft with the batch-ready label refused by
build, a rename out of a HIGH path planned as standard, a plan that tiers PRs without the gate
file. The pure plan logic is in test_merge_batch_plan.py, land in test_merge_batch_land.py, the
crash recovery and the build rules in test_merge_batch_build.py.

`gh pr checks` has no --json in gh 2.45 (checked against the real gh), so the stub serves the
piped text format (name, bucket, elapsed, url) and refuses --json.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

from merge_batch_env import SHARED, ROOT, conflicting, env_fixture, first_parent, two

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")


@pytest.fixture
def env(tmp_path):
    yield from env_fixture(tmp_path)


def test_guard_real_gh_unreachable(env):
    """Fails if ANY PATH entry offers a gh other than the stub (D-852)."""
    hits = [d for d in env.env["PATH"].split(":") if (Path(d) / "gh").exists()]
    assert hits == [str(env.stubs)]
    res = env.run("plan", "--prs", "99")  # an unknown PR reaches the stub, which refuses it
    assert res.returncode == 1 and "no pull request 99" in res.stderr


# ------------------------------------------------------------------------------------- plan
def test_plan_cli_label_json_and_table(env):
    env.branch_pr(1, {"a.py": "a\n"}, lines=30)
    env.branch_pr(2, {"src/plangraph/keeper/x.py": "x\n"}, lines=40, title="keeper change")
    env.branch_pr(3, {"c.py": "c\n"}, labels=())
    env.branch_pr(4, {"d.py": "d\n"}, ci="red")
    env.branch_pr(5, {".github/workflows/tests.yml": "on: push\n"}, lines=20)
    env.branch_pr(6, {"f.py": "f\n"}, draft=True, labels=())  # a draft without the label
    env.branch_pr(7, {"g.py": "g\n"}, draft=True)  # a draft that carries batch-ready
    res = env.run("plan", "--json")
    plan = json.loads(res.stdout)
    assert res.returncode == 0 and plan["repo"] == "owner/proj" and plan["label"] == "batch-ready"
    assert [(b["tier"], b["prs"]) for b in plan["batches"]] == [("STANDARD", [1, 7]), ("HIGH", [2, 5])]
    assert {r["number"]: r["reason"] for r in plan["prs"] if r["reason"]} == {4: "ci red"}
    assert [r["draft"] for r in plan["prs"] if r["number"] == 7] == [True]
    only = json.loads(env.run("plan", "--prs", "6", "--json").stdout)
    assert only["batches"] == [] and only["prs"][0]["reason"] == "draft"
    table = env.run("plan", "--prs", "1,2,3", "--repo", "owner/proj").stdout
    assert "#3" in table and "batch 1 STANDARD: --prs 1,3" in table and "batch 2 HIGH: --prs 2" in table
    assert "HIGH via src/plangraph/keeper/x.py" in table
    assert env.run("plan", "--max-prs", "0").returncode == 2


def test_plan_high_by_the_old_path_of_a_rename(env):
    keeper = "src/plangraph/keeper/store.py"
    env.seed({keeper: "x = 1\n"})
    env.branch_pr(1, {}, moves={keeper: "docs/store.py"})  # gh lists only docs/store.py
    env.branch_pr(2, {"c.txt": "c\n"})
    plan = json.loads(env.run("plan", "--json").stdout)
    rows = {row["number"]: row for row in plan["prs"]}
    assert rows[1]["paths"] == ["docs/store.py"] and rows[1]["high"] is True and rows[1]["high_paths"] == [keeper]
    assert [(b["tier"], b["prs"]) for b in plan["batches"]] == [("HIGH", [1]), ("STANDARD", [2])]
    assert "HIGH via " + keeper in env.run("plan").stdout
    assert env.git("for-each-ref", "refs/merge-batch") == "" and not (env.repo / ".git" / "FETCH_HEAD").exists()


def test_plan_refuses_when_it_cannot_read_the_pr_paths(env):
    env.add_pr(1, "lane/p1", files=["docs/a.md"])  # no refs/pull/1/head in the origin
    res = env.run("plan", "--prs", "1")
    assert res.returncode == 1 and "cannot fetch the PR heads" in res.stderr and res.stdout == ""


def test_plan_refuses_without_the_gate_file_even_with_high_paths(env, tmp_path):
    """merge_batch copied away from tools/ci_commit_gate.py: the AUTH_DOOR list is unknown, --high-path is no stand-in."""
    copy = tmp_path / "copy"
    copy.mkdir()
    for path in (ROOT / "tools").glob("merge_batch*.py"):
        shutil.copy(path, copy)
    env.branch_pr(1, {"docs/a.md": "a\n"})
    ok = env.run("plan", "--prs", "1", "--high-path", "docs/*")
    assert ok.returncode == 0 and "HIGH via docs/a.md" in ok.stdout
    for extra in ((), ("--high-path", "docs/*")):
        res = env.run("plan", "--prs", "1", *extra, tool=copy / "merge_batch.py")
        assert res.returncode == 1 and "HIGH path list is unknown" in res.stderr and res.stdout == ""
    res = env.run("build", "--name", "g1", "--prs", "1", "--high-path", "docs/*", tool=copy / "merge_batch.py")
    assert res.returncode == 1 and "HIGH path list is unknown" in res.stderr and not env.state_file("g1").exists()


# ------------------------------------------------------------------------------------ build
def test_build_clean(env):
    env.branch_pr(1, {"a.txt": "a\n"}, item="ITEM-A", title="add a")
    env.branch_pr(2, {"b.txt": "b\n"})
    env.branch_pr(3, {"c.txt": "c\n"}, ref="lane/c")
    res = env.run("build", "--name", "b1", "--prs", "1,2,3")
    assert res.returncode == 0, res.stderr
    wt = env.tmp / "repo-worktrees" / "batch-b1"
    assert first_parent(env, "batch/b1", wt) == [
        "Merge PR #3: change 3", "Merge PR #2: change 2", "Merge PR #1: add a", "seed"]
    assert env.git("rev-list", "--merges", "--count", "batch/b1") == "3"
    assert [(wt / n).read_text() for n in ("a.txt", "b.txt", "c.txt")] == ["a\n", "b\n", "c\n"]
    assert env.git("merge-base", "batch/b1", "origin/main") == env.git("rev-parse", "origin/main")
    assert not (env.repo / "a.txt").exists() and env.git("status", "--porcelain") == ""  # live tree untouched
    st = json.loads(env.state_file("b1").read_text())
    assert st["status"] == "built" and st["head"] == env.git("rev-parse", "batch/b1")
    assert [(m["pr"], m["items"]) for m in st["merged"]] == [(1, ["plangraph:ITEM-A"]), (2, []), (3, [])]
    assert env.run("status", cwd=wt).stdout.startswith("b1  built")  # state is found from the worktree too


def test_build_stack_order_and_refusals(env):
    env.branch_pr(4, {"a.txt": "a\n"}, ref="lane/s4")
    env.branch_pr(5, {"b.txt": "b\n"}, base="lane/s4", ref="lane/s5")
    env.branch_pr(6, {"c.txt": "c\n"}, draft=True, labels=())
    env.add_pr(7, "lane/p7", base="release")
    assert "draft without the batch-ready label" in env.run("build", "--name", "r0", "--prs", "4,6").stderr
    assert "#7 -> 'release'" in env.run("build", "--name", "r0", "--prs", "4,7").stderr
    assert "bad batch name" in env.run("build", "--name", "../x", "--prs", "4").stderr
    assert env.run("build", "--name", "r1", "--prs", "5,4").returncode == 0  # base PR first, whatever the order
    assert first_parent(env, "batch/r1")[:2] == ["Merge PR #5: change 5", "Merge PR #4: change 4"]
    again = env.run("build", "--name", "r1", "--prs", "4")
    assert again.returncode == 1 and "already in progress" in again.stderr


def test_build_accepts_a_batch_ready_draft(env):
    env.branch_pr(1, {"a.txt": "a\n"}, draft=True)  # a draft, labelled batch-ready by default
    res = env.run("build", "--name", "d1", "--prs", "1")
    assert res.returncode == 0, res.stderr
    assert (env.tmp / "repo-worktrees" / "batch-d1" / "a.txt").read_text() == "a\n"


def test_conflict_stop_continue_and_push(env):
    conflicting(env)
    wt = env.tmp / "wt"
    res = env.run("build", "--name", "c1", "--prs", "1,2,3", "--worktree", str(wt))
    assert res.returncode == 3 and "CONFLICT PR #2" in res.stdout and "  shared.txt" in res.stdout
    assert "continue --name c1" in res.stdout
    st = json.loads(env.state_file("c1").read_text())
    assert (st["status"], st["pending"], st["current"]["files"]) == ("conflict", [3], ["shared.txt"])
    early = env.run("continue", "--name", "c1")
    assert early.returncode == 1 and "still in progress" in early.stderr
    (wt / "shared.txt").write_text(two("line 3 both"))
    env.git("add", "shared.txt", cwd=wt)
    env.git("commit", "-q", "--no-edit", cwd=wt)
    res = env.run("continue", "--name", "c1")
    assert res.returncode == 0, res.stderr
    st = json.loads(env.state_file("c1").read_text())
    assert st["status"] == "built" and [m["pr"] for m in st["merged"]] == [1, 2, 3]
    assert (st["merged"][1]["resolved"], st["merged"][1]["files"]) == (True, ["shared.txt"])
    assert first_parent(env, "batch/c1", wt)[:2] == ["Merge PR #3: change 3", "Merge PR #2: change 2"]
    assert env.run("push", "--name", "c1").stdout.strip() == "100"
    body = env.load()["prs"]["100"]["body"]
    assert "Resolution commits" in body and "PR #2: `shared.txt`" in body


def test_union_paths_do_not_conflict(env):
    env.branch_pr(1, {"shared.txt": SHARED.replace("line 1\n", "line 1\nadd one\n")})
    env.branch_pr(2, {"shared.txt": SHARED.replace("line 1\n", "line 1\nadd two\n")})
    assert env.run("build", "--name", "u0", "--prs", "1,2").returncode == 3  # same spot: a conflict
    assert env.run("abort", "--name", "u0").returncode == 0
    res = env.run("build", "--name", "u1", "--prs", "1,2", "--union", "shared.txt")
    assert res.returncode == 0, res.stdout + res.stderr
    text = (env.tmp / "repo-worktrees" / "batch-u1" / "shared.txt").read_text()
    assert "add one\n" in text and "add two\n" in text and text.count("line ") == 6
    assert env.state_file("u1").with_suffix(".attrs").exists()
    assert env.run("abort", "--name", "u1").returncode == 0 and not env.state_file("u1").with_suffix(".attrs").exists()


def test_repo_gitattributes_stop_changelog_conflicts(env):
    """The repo's own .gitattributes (CHANGELOG.md merge=union): two PRs that each add the first changelog
    line conflict in a batch build without it and merge clean once the base carries it; no --union."""
    attrs = (ROOT / ".gitattributes").read_text()
    assert "CHANGELOG.md merge=union" in attrs.splitlines() and "tools/fleet-msg text eol=lf" in attrs.splitlines()
    ledger = "# Changelog\n\n## Unreleased\n\n- old entry\n"
    (env.repo / "CHANGELOG.md").write_text(ledger)
    env.git("add", "-A")
    env.git("commit", "-q", "-m", "changelog")
    env.git("push", "-q", "origin", "main")
    env.branch_pr(1, {"CHANGELOG.md": ledger.replace("\n- old", "\n- one\n- old")})
    env.branch_pr(2, {"CHANGELOG.md": ledger.replace("\n- old", "\n- two\n- old")})
    assert env.run("build", "--name", "g0", "--prs", "1,2").returncode == 3  # no attribute yet: a conflict
    assert env.run("abort", "--name", "g0").returncode == 0
    (env.repo / ".gitattributes").write_text(attrs)
    env.git("add", ".gitattributes")
    env.git("commit", "-q", "-m", "union the changelog")
    env.git("push", "-q", "origin", "main")
    res = env.run("build", "--name", "g1", "--prs", "1,2")
    assert res.returncode == 0, res.stdout + res.stderr
    text = (env.tmp / "repo-worktrees" / "batch-g1" / "CHANGELOG.md").read_text()
    assert "- one\n" in text and "- two\n" in text and text.count("- old entry") == 1
    assert "warning" not in res.stderr  # both PRs only add lines: no old line was touched


def test_skip_conflicts_labels_and_continues(env):
    conflicting(env)
    res = env.run("build", "--name", "s1", "--prs", "1,2,3", "--skip-conflicts")
    assert res.returncode == 0 and "skipped #2 (conflict)" in res.stdout
    wt = env.tmp / "repo-worktrees" / "batch-s1"
    st = json.loads(env.state_file("s1").read_text())
    assert [m["pr"] for m in st["merged"]] == [1, 3] and st["status"] == "built"
    assert (st["skipped"][0]["pr"], st["skipped"][0]["files"]) == (2, ["shared.txt"])
    assert env.git("status", "--porcelain", cwd=wt) == ""
    assert not Path(env.git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD", cwd=wt)).exists()
    label = ["api", "--method", "POST", "repos/owner/proj/issues/2/labels", "-f", "labels[]=needs-update"]
    assert env.calls("api") == [label, label]  # the first try finds no such label, the retry follows its creation
    assert env.calls("label", "create")[0][2] == "needs-update" and not env.calls("pr", "edit")
    assert {"name": "needs-update"} in env.load()["prs"]["2"]["labels"]


def test_skip_conflicts_survives_a_label_it_cannot_add(env):
    conflicting(env)
    env.state["label_fail"] = True
    env.save()
    res = env.run("build", "--name", "s2", "--prs", "1,2,3", "--skip-conflicts")
    assert res.returncode == 0 and "could not add the needs-update label to PR #2" in res.stderr
    assert json.loads(env.state_file("s2").read_text())["skipped"][0]["labeled"] is False


def test_push_opens_batch_pr_and_updates_it(env):
    conflicting(env)
    assert env.run("build", "--name", "p1", "--prs", "1,2,3", "--skip-conflicts").returncode == 0
    env.load()["create_noise"] = True
    env.save()
    res = env.run("push", "--name", "p1")
    assert res.returncode == 0 and res.stdout == "100\n"
    st = json.loads(env.state_file("p1").read_text())
    assert (st["status"], st["pr"]) == ("pushed", 100)
    assert env.git("rev-parse", "refs/heads/batch/p1", cwd=env.origin) == st["head"]
    pr = env.load()["prs"]["100"]
    assert (pr["title"], pr["baseRefName"], pr["headRefName"]) == ("batch p1: #1 #3", "main", "batch/p1")
    assert "| #1 | plangraph:ITEM-A |" in pr["body"] and "| edit one |" in pr["body"]
    assert "Skipped" in pr["body"] and "#2" in pr["body"] and "`shared.txt`" in pr["body"]
    again = env.run("push", "--name", "p1")
    patch = env.calls("api", "--method", "PATCH")
    assert again.stdout == "100\n" and len(env.calls("pr", "create")) == 1 and len(patch) == 1
    assert patch[0][3] == "repos/owner/proj/pulls/100" and env.load()["prs"]["100"]["body"] == pr["body"]


def test_status_and_abort(env):
    env.branch_pr(1, {"a.txt": "a\n"})
    assert env.run("status").stdout == "no batch in progress\n"
    assert env.run("build", "--name", "x1", "--prs", "1").returncode == 0
    wt = env.tmp / "repo-worktrees" / "batch-x1"
    assert env.run("status").stdout.startswith("x1  built  base=main  merged=1 skipped=0 pending=0")
    assert json.loads(env.run("status", "--json").stdout)[0]["name"] == "x1"
    assert env.run("abort", "--name", "x1").returncode == 0
    assert not wt.exists() and not env.state_file("x1").exists() and env.git("branch", "--list", "batch/x1") == ""
    assert env.run("continue", "--name", "x1").returncode == 1
