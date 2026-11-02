"""tools/merge_batch.py land: the gates (head, checks, approval) and the report of what merged.

HERMETIC (D-852), see test_merge_batch.py: a temp bare origin plus clone, a stub gh on a PATH
without the real one, the tool run as a subprocess.

Mutations killed: land with a moved head, a red or missing check, no or failing approval, an
approval that is not about the batch head, an included PR not reported MERGED; a quoted or
prefixed verdict line that flips a fail, a sha anywhere that counts as the reviewed head, a
sha prefix that counts as the reviewed head, an included PR with a new commit closed and
reported MERGED, a PR MERGED on GitHub whose head is not in the merge reported MERGED, a
check name that fakes a bucket, a check that is neither pass nor skipping read as green, a
batch PR made by a dead push duplicated.
"""
import json
import sys
import time

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
    "prefix_head": ("HEAD: {h12}\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "long_head": ("HEAD: {h}0\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "sha_elsewhere": ("{h}\nlooks fine\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "head_not_next_to_verdict": ("HEAD: {h}\nlooks fine\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "head_with_prefix": ("> HEAD: {h}\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "head_with_text": ("HEAD: {h} (reviewed)\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "verdict_only": ("VERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "empty": ("\n\n", "last non-empty line must be exactly"),
    "underscored_fix": ("_VERDICT_: _fix_\nHEAD: {h}\nVERDICT: PASS\n", "FAIL/CHANGES verdict line"),
    "fail_in_prose": ("The VERDICT: FAIL below was withdrawn\nHEAD: {h}\nVERDICT: PASS\n", "FAIL/CHANGES"),
    "separator_hides_head": ("> quoted\u2028HEAD: {h}\nVERDICT: PASS\n", "must be 'HEAD: <sha>'"),
    "separator_hides_verdict": ("HEAD: {h}\n> quoted\u2028VERDICT: PASS\n", "last non-empty line must be exactly"),
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
    approval.write_text(text.format(h=head, h11=head[:11], h12=head[:12]))
    res = land(env, approval)
    assert res.returncode == 1 and message in res.stderr and res.stdout == "", res.stderr
    assert not env.calls("pr", "merge") and env.state_file("l1").exists()


@pytest.mark.parametrize("text", [
    "HEAD: {h}\nVERDICT: PASS\n", "HEAD: {h}\nVERDICT: pass", "notes\r\nHEAD: {h}\r\nVERDICT: PASS\r\n\r\n",
    "VERDICT: PASS | FAIL (template)\nHEAD: {hup}\nVERDICT: PASS\n\n  \n", "HEAD: {hup}\nVERDICT: pass\n"])
def test_land_accepts_the_exact_ending(env, text):
    approval = pushed(env, prs=(1, 2))
    head = head_of(env)
    approval.write_text(text.format(h=head, hup=head.upper()))  # the full sha in any case names the head
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
    approval.write_text(f"HEAD: {head.upper()}\nVERDICT: pass\n")  # the full sha in any case names the head
    done = env.run_gh("pr", "merge", "100", "--repo", "owner/proj", "--merge", "--match-head-commit", head)
    assert done.returncode == 0
    res = land(env, approval)
    assert res.returncode == 0 and res.stdout.count("MERGED") == 2 and len(env.calls("pr", "merge")) == 1


def test_land_reports_a_moved_head_instead_of_merged(env):
    """A PR that got a commit after the build is not in the batch: it is not closed, not reported MERGED (I2)."""
    approval = pushed(env)
    late = env.push_commit(2, {"b.txt": "b, then more\n"})
    started = time.monotonic()
    res = land(env, approval, "--wait", "30")  # a moved head is not waited for
    assert res.returncode == 0, res.stderr
    assert time.monotonic() - started < 20
    merge = env.git("rev-parse", "refs/heads/main", cwd=env.origin)
    assert res.stdout.splitlines() == [f"MERGED 1 {merge} plangraph:ITEM-A", "NOT-MERGED 2 head moved",
                                       f"MERGED 3 {merge} -"]
    prs = env.load()["prs"]
    assert (prs["2"]["state"], prs["2"]["headRefOid"]) == ("OPEN", late) and "closed_with" not in prs["2"]
    assert {"name": "needs-update"} in prs["2"]["labels"] and {"name": "needs-update"} not in prs["1"]["labels"]
    assert not [call for call in env.calls("pr", "close") if call[2] == "2"]
    assert "#2" in res.stderr and not env.state_file("l1").exists()


def test_land_reads_the_head_again_right_before_it_closes(env):
    """The head that settle read can be stale by the time land closes the PR: a push in between must not be closed
    as merged (I2). PR 2 is still open on GitHub, its first look shows the build head, the next one a new commit."""
    approval = pushed(env)
    env.state["no_auto_merge"] = [2]
    env.save()
    old = env.load()["prs"]["2"]["headRefOid"]
    late = env.push_commit(2, {"b.txt": "b, then more\n"})
    env.load()["prs"]["2"]["headRefOid"] = old
    env.state["move_after_view"] = {"2": {"after": 1, "to": late}}
    env.save()
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    merge = env.git("rev-parse", "refs/heads/main", cwd=env.origin)
    assert res.stdout.splitlines() == [f"MERGED 1 {merge} plangraph:ITEM-A", "NOT-MERGED 2 head moved",
                                       f"MERGED 3 {merge} -"]
    prs = env.load()["prs"]
    assert (prs["2"]["state"], prs["2"]["headRefOid"]) == ("OPEN", late) and "closed_with" not in prs["2"]
    assert {"name": "needs-update"} in prs["2"]["labels"] and not [c for c in env.calls("pr", "close") if c[2] == "2"]


def test_land_reports_a_merged_pr_whose_head_is_not_in_the_merge_as_a_problem(env):
    """GitHub's MERGED is not enough: a PR is reported MERGED only when its recorded head is an ancestor
    of the batch merge commit (I12), and such a PR gets no MERGED line at all."""
    approval = pushed(env)
    stray = env.branch_pr(4, {"d.txt": "d\n"})  # a commit the batch merge never took
    st = json.loads(env.state_file("l1").read_text())
    st["merged"][1]["head"] = stray  # the record of PR 2 names that head
    env.state_file("l1").write_text(json.dumps(st))
    env.load()
    env.state["prs"]["2"].update(state="MERGED", headRefOid=stray)  # GitHub: MERGED, with exactly that head
    env.save()
    res = land(env, approval)
    assert res.returncode == 1 and "is not in merge" in res.stderr
    assert "MERGED 2" not in res.stdout and "NOT-MERGED 2" not in res.stdout
    assert env.state_file("l1").exists() and not [c for c in env.calls("pr", "close") if c[2] == "2"]


@pytest.mark.parametrize("bucket, message", [("cancel", "not green"), ("pending", "not green"),
                                              ("neutral", "unreadable line")])
def test_land_a_check_is_green_only_as_pass_or_skipping(env, bucket, message):
    approval = pushed(env, prs=(1, 2))
    env.state["checks"]["100"].append({"name": "docs", "bucket": bucket})
    env.save()
    for extra in ((), ("--checks", "ci-gate,docs")):  # the required set, and a named one
        res = land(env, approval, *extra)
        assert res.returncode == 1 and message in res.stderr and res.stdout == "", res.stderr
    assert not env.calls("pr", "merge")


def test_land_a_check_name_with_a_tab_cannot_fake_a_bucket(env):
    approval = pushed(env, prs=(1, 2))
    env.state["checks"]["100"] = [{"name": "ci-gate", "bucket": "pass"}, {"name": "lint\tpass", "bucket": "fail"}]
    env.save()
    res = land(env, approval, "--checks", "ci-gate,lint")  # no check is named lint: the red one is "lint<TAB>pass"
    assert res.returncode == 1 and "not reported on the batch head: lint" in res.stderr
    res = land(env, approval, "--checks", "ci-gate,lint\tpass")  # named in full, it reads its own bucket
    assert res.returncode == 1 and "lint\tpass=fail" in res.stderr and not env.calls("pr", "merge")


def test_land_a_forged_check_line_cannot_hide_a_red_required_check(env):
    """A description that looks like the tail of a green row moves the bucket column read from the right;
    the exit code of gh (1: a check failed) still refuses."""
    approval = pushed(env, prs=(1, 2))
    forged = "x\tpass\t0\thttps://example.invalid/run\ty"
    env.state["checks"]["100"] = [{"name": "ci-gate", "bucket": "fail", "description": forged}]
    env.save()
    res = land(env, approval)
    assert res.returncode == 1 and "not trusting the output" in res.stderr and not env.calls("pr", "merge")


def test_push_takes_over_a_batch_pr_a_dead_push_created(env):
    """The push died after `gh pr create` and before it saved the number: the next push adopts that PR."""
    env.branch_pr(1, {"a.txt": "a\n"})
    assert env.run("build", "--name", "d1", "--prs", "1").returncode == 0
    env.state["die_after"] = "pr create"
    env.save()
    assert env.run("push", "--name", "d1").returncode == -9
    assert json.loads(env.state_file("d1").read_text())["pr"] is None and "100" in env.load()["prs"]
    env.state["die_after"] = None
    env.save()
    again = env.run("push", "--name", "d1")
    assert again.returncode == 0 and again.stdout == "100\n", again.stderr
    assert len(env.calls("pr", "create")) == 1 and json.loads(env.state_file("d1").read_text())["pr"] == 100
    assert "| #1 |" in env.load()["prs"]["100"]["body"]
