"""tools/merge_batch.py: plan, build, continue, push, land, status and abort.

HERMETIC (D-852): every repo is a temp bare origin plus clone, `gh` is a stub on a PATH that
holds only git, sh, env and python3 (it serves canned JSON and records its calls), and the tool
runs as a subprocess with a minimal env. `test_guard_real_gh_unreachable` fails if a real gh
could run; the `env` fixture fails any test whose tool called a gh command the stub lacks.

Mutations killed: stacked PR before its base, HIGH PR sharing a batch or a stack above it
escaping the tier or a file list gh cut short read as safe, caps ignored or a stack split, a
draft/red/needs-update PR batched, a conflict merged blind or left half merged, a skipped PR not
labelled, a --union path still conflicting, land with a moved head, a red or missing check, no or
failing approval, an included PR not reported MERGED.

`gh pr checks` has no --json in gh 2.45 (checked against the real gh), so the stub serves the
piped text format (name, bucket, elapsed, url) and refuses --json.
"""
import json
import sys
from pathlib import Path

import pytest

from merge_batch_env import ROOT, Env, pr_json

sys.path.insert(0, str(ROOT / "tools"))

from merge_batch_plan import high_patterns, plan_batches, stack_parents  # noqa: E402
from merge_batch_util import MergeBatchError, norm_pr, run  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")
SHARED = "".join(f"line {i}\n" for i in range(1, 7))


@pytest.fixture
def env(tmp_path):
    fixture = Env(tmp_path)
    yield fixture
    assert not (fixture.stubs / "UNEXPECTED").exists(), "the tool called a gh command the stub lacks"


def P(number, ref=None, **kw):
    return norm_pr(pr_json(number, ref or f"lane/p{number}", **kw))


def two(text):
    return SHARED.replace("line 3", text)


def conflicting(env):
    """PR 1 and 2 edit the same line, 3 is independent."""
    env.branch_pr(1, {"shared.txt": two("line 3 one")}, item="ITEM-A", title="edit one")
    env.branch_pr(2, {"shared.txt": two("line 3 two")}, item="ITEM-B")
    env.branch_pr(3, {"c.txt": "c\n"})


def first_parent(env, ref, cwd=None):
    return env.git("log", "--first-parent", "--format=%s", ref, cwd=cwd).splitlines()


def test_guard_real_gh_unreachable(env):
    """Fails if ANY PATH entry offers a gh other than the stub (D-852)."""
    hits = [d for d in env.env["PATH"].split(":") if (Path(d) / "gh").exists()]
    assert hits == [str(env.stubs)]
    res = env.run("plan", "--prs", "99")  # an unknown PR reaches the stub, which refuses it
    assert res.returncode == 1 and "no pull request 99" in res.stderr


# ------------------------------------------------------------------------------------- plan
def test_plan_stack_order():
    prs = [P(12, base="lane/p10"), P(11), P(10)]  # 12 is based on PR 10's branch
    plan = plan_batches(prs, "main")
    assert [r["number"] for r in plan["prs"]] == [10, 12, 11]
    assert plan["prs"][1]["after"] == 10 and plan["batches"][0]["prs"] == [10, 12, 11]
    assert stack_parents([P(1, ref="main", cross=True), P(2)]) == {}  # a fork's main is no stack parent
    loop = plan_batches([P(1, base="lane/p2"), P(2, base="lane/p1")], "main")
    assert [r["reason"] for r in loop["prs"]] == ["stack cycle", "stack cycle"]


def test_plan_high_split():
    prs = [P(1, files=["docs/a.md"]), P(2, files=["src/plangraph/keeper/store.py"]), P(3, files=["tools/x.py"]),
           P(4, base="lane/p2", files=["docs/b.md"]), P(5, files=[".github/workflows/tests.yml"]),
           P(6, files=["tools/ci_commit_gate.py"])]
    plan = plan_batches(prs, "main", patterns=high_patterns())
    assert [(b["tier"], b["prs"]) for b in plan["batches"]] == [
        ("STANDARD", [1, 3]), ("HIGH", [2, 4]), ("HIGH", [5]), ("HIGH", [6])]
    by_num = {r["number"]: r for r in plan["prs"]}
    assert by_num[4]["tier"] == "HIGH" and by_num[4]["high"] is False  # stacked on a HIGH PR
    assert by_num[2]["high_paths"] == ["src/plangraph/keeper/store.py"] and by_num[1]["high_paths"] == []
    assert high_patterns(("docs/*",)) != high_patterns()
    cut = plan_batches([P(7, files=["docs/a.md"], changed=300)], "main", patterns=high_patterns())  # gh cut the list
    assert cut["batches"][0]["tier"] == "HIGH" and cut["prs"][0]["high_paths"] == ["(file list truncated by gh)"]


def test_plan_caps():
    prs = [P(n, lines=100) for n in range(1, 6)]
    assert [b["prs"] for b in plan_batches(prs, "main", max_prs=2)["batches"]] == [[1, 2], [3, 4], [5]]
    assert [b["prs"] for b in plan_batches(prs, "main", max_lines=250)["batches"]] == [[1, 2], [3, 4], [5]]
    prs = [P(1, lines=100), P(2, lines=100), P(3, base="lane/p2", lines=100), P(4, lines=900)]
    plan = plan_batches(prs, "main", max_prs=2, max_lines=500)  # the 2+3 stack is never split
    assert [b["prs"] for b in plan["batches"]] == [[1], [2, 3], [4]]
    assert [b["over_cap"] for b in plan["batches"]] == [False, False, True]


def test_plan_excludes():
    prs = [P(1, draft=True), P(2, ci="red"), P(3, labels=("batch-ready", "needs-update")), P(4, base="release"),
           P(5, base="lane/p1"), P(6, ci="pending"), P(7, ci="none"), P(8, state="CLOSED")]
    plan = plan_batches(prs, "main")
    why = {r["number"]: r["reason"] for r in plan["prs"] if r["reason"]}
    assert why == {1: "draft", 2: "ci red", 3: "label needs-update", 4: "targets 'release', not 'main'",
                   5: "stacked on #1 (draft)", 8: "not open (closed)"}
    assert [b["prs"] for b in plan["batches"]] == [[6, 7]]


def test_a_hung_command_is_an_error_not_a_hang():
    with pytest.raises(MergeBatchError, match="did not finish in 1s"):
        run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    with pytest.raises(MergeBatchError, match="cannot run"):
        run(["/nonexistent/merge-batch-tool"])


def test_pr_head_must_be_a_sha():
    with pytest.raises(MergeBatchError, match="not a commit sha"):
        norm_pr(pr_json(1, "lane/p1", head="--upload-pack=x"))


def test_plan_cli_label_json_and_table(env):
    env.add_pr(1, "lane/p1", files=["a.py"], lines=30)
    env.add_pr(2, "lane/p2", files=["src/plangraph/keeper/x.py"], lines=40, title="keeper change")
    env.add_pr(3, "lane/p3", labels=(), files=["c.py"])
    env.add_pr(4, "lane/p4", draft=True, files=["d.py"])
    res = env.run("plan", "--json")
    plan = json.loads(res.stdout)
    assert res.returncode == 0 and plan["repo"] == "owner/proj" and plan["label"] == "batch-ready"
    assert [(b["tier"], b["prs"]) for b in plan["batches"]] == [("STANDARD", [1]), ("HIGH", [2])]
    assert {r["number"]: r["reason"] for r in plan["prs"] if r["reason"]} == {4: "draft"}
    table = env.run("plan", "--prs", "1,2,3", "--repo", "owner/proj").stdout
    assert "#3" in table and "batch 1 STANDARD: --prs 1,3" in table and "batch 2 HIGH: --prs 2" in table
    assert "HIGH via src/plangraph/keeper/x.py" in table
    assert env.run("plan", "--max-prs", "0").returncode == 2


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
    env.branch_pr(6, {"c.txt": "c\n"}, draft=True)
    env.add_pr(7, "lane/p7", base="release")
    assert "draft" in env.run("build", "--name", "r0", "--prs", "4,6").stderr
    assert "one base branch" in env.run("build", "--name", "r0", "--prs", "4,7").stderr
    assert "bad batch name" in env.run("build", "--name", "../x", "--prs", "4").stderr
    assert env.run("build", "--name", "r1", "--prs", "5,4").returncode == 0  # base PR first, whatever the order
    assert first_parent(env, "batch/r1")[:2] == ["Merge PR #5: change 5", "Merge PR #4: change 4"]
    again = env.run("build", "--name", "r1", "--prs", "4")
    assert again.returncode == 1 and "already in progress" in again.stderr


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


# ------------------------------------------------------------------------------------- land
def pushed(env, name="l1", prs=(1, 2, 3), checks="pass"):
    """A pushed batch of PRs 1..3 whose batch PR is #100 with green checks and a passing approval."""
    env.branch_pr(1, {"a.txt": "a\n"}, item="ITEM-A")
    env.branch_pr(2, {"b.txt": "b\n"}, item="ITEM-B")
    env.branch_pr(3, {"c.txt": "c\n"})
    assert env.run("build", "--name", name, "--prs", ",".join(map(str, prs))).returncode == 0
    assert env.run("push", "--name", name).stdout == "100\n"
    env.load()  # the stub created PR 100
    env.state["checks"]["100"] = [{"name": "ci-gate", "bucket": checks}]
    env.save()
    approval = env.tmp / "approval.txt"
    approval.write_text("looks fine\nVERDICT: pass\n")
    return approval


def land(env, approval, *extra, name="l1", number="100"):
    return env.run("land", "--name", name, "--batch-pr", number, "--approval", str(approval), "--wait", "0", *extra)


@pytest.mark.parametrize("case, message", [
    ("head_moved", "head moved"), ("red_check", "not green"), ("pending_check", "not green"),
    ("no_checks", "no required check"), ("no_approval", "cannot be read"), ("fix_verdict", "no passing VERDICT"),
    ("no_verdict", "no passing VERDICT"), ("wrong_pr", "push it first"), ("draft", "draft"),
    ("closed", "is CLOSED")])
def test_land_refusals(env, case, message):
    approval = pushed(env)
    batch = env.state["prs"]["100"]
    checks = env.state["checks"]["100"][0]
    if case == "head_moved":
        batch["headRefOid"] = "f" * 40
    elif case == "red_check":
        checks["bucket"] = "fail"
    elif case == "pending_check":
        checks["bucket"] = "pending"
    elif case == "no_checks":
        env.state["checks"]["100"] = []
    elif case == "no_approval":
        approval.unlink()
    elif case in ("fix_verdict", "no_verdict"):
        approval.write_text("VERDICT: fix\n" if case == "fix_verdict" else "all good\n")
    elif case == "draft":
        batch["isDraft"] = True
    elif case == "closed":
        batch["state"] = "CLOSED"
    env.save()
    res = land(env, approval, number="101" if case == "wrong_pr" else "100")
    assert res.returncode == 1 and message in res.stderr and res.stdout == ""
    assert not env.calls("pr", "merge") and env.state_file("l1").exists()
    if case == "no_approval":
        assert "--approval" in env.run("land", "--name", "l1", "--batch-pr", "100").stderr


def test_land_merges_closes_unmerged_and_reports(env):
    approval = pushed(env)
    env.state["no_auto_merge"] = [3]  # GitHub has not marked PR 3 merged
    env.save()
    head = json.loads(env.state_file("l1").read_text())["head"]
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    merge = env.git("rev-parse", "refs/heads/main", cwd=env.origin)
    assert res.stdout.splitlines() == [f"MERGED 1 {merge} plangraph:ITEM-A", f"MERGED 2 {merge} plangraph:ITEM-B",
                                       f"MERGED 3 {merge} -"]
    prs = env.load()["prs"]
    assert (prs["1"]["state"], prs["3"]["state"], prs["3"]["closed_with"]) == ("MERGED", "CLOSED",
                                                                               "merged via batch PR #100")
    assert env.calls("pr", "merge") == [["pr", "merge", "100", "--repo", "owner/proj", "--merge",
                                         "--match-head-commit", head]]
    assert env.calls("pr", "checks") == [["pr", "checks", "100", "--repo", "owner/proj", "--required"]]
    assert not env.state_file("l1").exists() and env.git("for-each-ref", "refs/merge-batch") == ""


def test_land_named_checks(env):
    approval = pushed(env, prs=(1, 2))
    env.state["checks"]["100"].append({"name": "lint", "bucket": "fail"})
    env.save()
    missing = land(env, approval, "--checks", "ci-gate,docs")
    assert missing.returncode == 1 and "not reported on the batch head: docs" in missing.stderr
    assert land(env, approval, "--checks", "ci-gate,lint").returncode == 1  # a named check is red
    ok = land(env, approval, "--checks", "ci-gate")  # --checks replaces the required set
    assert ok.returncode == 0 and ok.stdout.count("MERGED") == 2
    assert env.calls("pr", "checks")[-1] == ["pr", "checks", "100", "--repo", "owner/proj"]


def test_land_resumes_after_a_merge_made_elsewhere(env):
    approval = pushed(env, prs=(1, 2))
    head = json.loads(env.state_file("l1").read_text())["head"]
    done = env.run_gh("pr", "merge", "100", "--repo", "owner/proj", "--merge", "--match-head-commit", head)
    assert done.returncode == 0
    res = land(env, approval)
    assert res.returncode == 0 and res.stdout.count("MERGED") == 2 and len(env.calls("pr", "merge")) == 1


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
