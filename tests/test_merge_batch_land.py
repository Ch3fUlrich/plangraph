"""tools/merge_batch.py land: the gates (head, checks, approval) and the report of what merged.

HERMETIC (D-852), see test_merge_batch.py: a temp bare origin plus clone, a stub gh on a PATH
without the real one, the tool run as a subprocess.

Mutations killed: land with a moved head, a red or missing check, no or failing approval, an
approval that is not about the batch head, an included PR not reported MERGED.
"""
import json
import sys

import pytest

from merge_batch_env import env_fixture, head_of, land, pushed

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")


@pytest.fixture
def env(tmp_path):
    yield from env_fixture(tmp_path)


@pytest.mark.parametrize("case, message", [
    ("head_moved", "head moved"), ("red_check", "not green"), ("pending_check", "not green"),
    ("no_checks", "no required check"), ("no_approval", "cannot be read"), ("fix_verdict", "no passing VERDICT"),
    ("no_verdict", "no passing VERDICT"), ("no_sha", "does not name the batch head"),
    ("other_sha", "does not name the batch head"), ("short_sha", "does not name the batch head"),
    ("wrong_pr", "push it first"), ("draft", "draft"), ("closed", "is CLOSED")])
def test_land_refusals(env, case, message):
    approval = pushed(env)
    batch = env.state["prs"]["100"]
    checks = env.state["checks"]["100"][0]
    if case == "head_moved":
        batch.update(headRefOid="f" * 40, live=False)  # GitHub shows another head than the one pushed
    elif case == "red_check":
        checks["bucket"] = "fail"
    elif case == "pending_check":
        checks["bucket"] = "pending"
    elif case == "no_checks":
        env.state["checks"]["100"] = []
    elif case == "no_approval":
        approval.unlink()
    elif case in ("fix_verdict", "no_verdict"):
        approval.write_text(head_of(env) + ("\nVERDICT: fix\n" if case == "fix_verdict" else "\nall good\n"))
    elif case in ("no_sha", "other_sha", "short_sha"):  # the approval must be about this very head
        named = {"no_sha": "", "other_sha": "1" * 40, "short_sha": head_of(env)[:11]}[case]
        approval.write_text(f"reviewed {named}\nVERDICT: pass\n")
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


def test_land_refuses_an_approval_for_an_older_head(env):
    approval = pushed(env, prs=(1, 2))
    old_head = head_of(env)
    wt = env.tmp / "repo-worktrees" / "batch-l1"
    (wt / "fix.txt").write_text("a fix after the review\n")
    env.git("add", "fix.txt", cwd=wt)
    env.git("commit", "-q", "-m", "fix after review", cwd=wt)
    assert env.run("push", "--name", "l1").stdout == "100\n"
    assert head_of(env) != old_head
    res = land(env, approval)  # the approval still names the head it reviewed
    assert res.returncode == 1 and f"does not name the batch head {head_of(env)}" in res.stderr
    assert not env.calls("pr", "merge")
    approval.write_text(f"{head_of(env)}\nVERDICT: accept\n")
    assert land(env, approval).returncode == 0 and len(env.calls("pr", "merge")) == 1


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
    head = head_of(env)
    approval.write_text(f"reviewed `{head[:12].upper()}`\nVERDICT: pass\n")  # 12 digits in any case name the head
    done = env.run_gh("pr", "merge", "100", "--repo", "owner/proj", "--merge", "--match-head-commit", head)
    assert done.returncode == 0
    res = land(env, approval)
    assert res.returncode == 0 and res.stdout.count("MERGED") == 2 and len(env.calls("pr", "merge")) == 1
