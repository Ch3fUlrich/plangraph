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


#: approval texts (``{h}`` = the batch head) that land must refuse, with the message it gives.
BAD_APPROVALS = {
    "fix_verdict": ("HEAD: {h}\nVERDICT: fix\n", "FAIL/CHANGES verdict line"),
    "no_verdict": ("HEAD: {h}\nall good\n", "last non-empty line must be exactly 'VERDICT: PASS'"),
    "quoted_pass_after_fix": ("VERDICT: fix\nHEAD: {h}\n> VERDICT: pass\n", "FAIL/CHANGES verdict line"),
    "prefixed_pass": ("HEAD: {h}\n> VERDICT: pass\n", "last non-empty line must be exactly"),
    "bulleted_pass": ("HEAD: {h}\n- VERDICT: PASS\n", "last non-empty line must be exactly"),
    "mixed_case_pass": ("HEAD: {h}\nVERDICT: Pass\n", "last non-empty line must be exactly"),
    "accept_word": ("HEAD: {h}\nVERDICT: accept\n", "last non-empty line must be exactly"),
    "text_after_verdict": ("HEAD: {h}\nVERDICT: PASS\nthanks\n", "last non-empty line must be exactly"),
    "changes_earlier": ("- VERDICT: CHANGES\nHEAD: {h}\nVERDICT: PASS\n", "FAIL/CHANGES verdict line"),
    "request_changes_earlier": ("**VERDICT**: REQUEST_CHANGES\nHEAD: {h}\nVERDICT: PASS\n", "FAIL/CHANGES"),
    "no_head_line": ("looks fine\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "other_head": ("HEAD: " + "1" * 40 + "\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "short_head": ("HEAD: {h11}\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "sha_elsewhere": ("{h}\nlooks fine\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "head_not_next_to_verdict": ("HEAD: {h}\nlooks fine\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "head_with_prefix": ("> HEAD: {h}\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "head_with_text": ("HEAD: {h} (reviewed)\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "verdict_only": ("VERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "empty": ("\n\n", "last non-empty line must be exactly"),
}


@pytest.mark.parametrize("case, message", [
    ("head_moved", "head moved"), ("red_check", "not green"), ("pending_check", "not green"),
    ("no_checks", "no required check"), ("no_approval", "cannot be read"),
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


@pytest.mark.parametrize("case", sorted(BAD_APPROVALS))
def test_land_refuses_a_bad_approval(env, case):
    """The approval ends in `HEAD: <sha>` and `VERDICT: PASS`; no fail verdict line anywhere."""
    approval = pushed(env, prs=(1, 2))
    text, message = BAD_APPROVALS[case]
    head = head_of(env)
    approval.write_text(text.format(h=head, h11=head[:11]))
    res = land(env, approval)
    assert res.returncode == 1 and message in res.stderr and res.stdout == "", res.stderr
    assert not env.calls("pr", "merge") and env.state_file("l1").exists()


@pytest.mark.parametrize("text", [
    "HEAD: {h}\nVERDICT: PASS\n", "HEAD: {h}\nVERDICT: pass", "notes\r\nHEAD: {h}\r\nVERDICT: PASS\r\n\r\n",
    "VERDICT: PASS | FAIL (template)\nHEAD: {h12}\nVERDICT: PASS\n\n  \n", "HEAD: {hup}\nVERDICT: pass\n"])
def test_land_accepts_the_exact_ending(env, text):
    approval = pushed(env, prs=(1, 2))
    head = head_of(env)
    approval.write_text(text.format(h=head, h12=head[:12], hup=head[:12].upper()))
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("MERGED") == 2 and len(env.calls("pr", "merge")) == 1


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
    assert res.returncode == 1 and f"naming the batch head {head_of(env)}" in res.stderr
    assert not env.calls("pr", "merge")
    approval.write_text(f"HEAD: {head_of(env)}\nVERDICT: PASS\n")
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
    approval.write_text(f"HEAD: {head[:12].upper()}\nVERDICT: pass\n")  # 12 digits in any case name the head
    done = env.run_gh("pr", "merge", "100", "--repo", "owner/proj", "--merge", "--match-head-commit", head)
    assert done.returncode == 0
    res = land(env, approval)
    assert res.returncode == 0 and res.stdout.count("MERGED") == 2 and len(env.calls("pr", "merge")) == 1
