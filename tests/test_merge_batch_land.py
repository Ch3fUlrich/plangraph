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
import re
import sys
import time
from pathlib import Path

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


@pytest.mark.parametrize("state, row, word", [
    ("red", None, "main_red"), ("pending", None, "main_pending"), ("unknown", None, "unknown"),
    ("ref_not_forward_only", None, "ref_not_forward_only"),
    ("pending", "EXCEPTION:main-red-fix", "main_pending"), ("unknown", "EXCEPTION:main-red-fix", "unknown")])
def test_land_refuses_when_main_is_not_green_and_the_exception_passes_nothing_but_red(env, state, row, word):
    approval = pushed(env)
    env.state["main_state"] = state
    env.save()
    extra = ()
    if row:
        go = env.tmp / "go.txt"
        go.write_text(f"GO D-1 {head_of(env)} {row}\n")
        extra = ("--go-row", str(go))
    res = land(env, approval, *extra)
    assert res.returncode != 0 and word in res.stderr and not env.calls("pr", "merge")


def test_land_asks_the_deploy_guard_with_a_fleet_refresh_before_it_merges(env):
    approval = pushed(env)
    env.state["deploy_guard"] = "deploy_ledger_missing: dell: no deploy-status ledger"
    env.save()
    res = land(env, approval)
    assert res.returncode != 0 and "deploy_ledger_missing" in res.stderr and not env.calls("pr", "merge")
    env.state["deploy_guard"] = "ok"  # the stub then also insists on `--refresh yes`
    env.save()
    assert land(env, approval).returncode == 0 and len(env.calls("pr", "merge")) == 1


def test_land_on_a_red_main_merges_only_with_the_main_red_fix_row(env):
    approval = pushed(env)
    env.state["main_state"] = "red"
    env.save()
    go = env.tmp / "go.txt"
    go.write_text(f"GO D-1 {head_of(env)} EXCEPTION:main-red-fix\n")
    res = land(env, approval, "--go-row", str(go))
    assert res.returncode == 0, res.stderr
    assert len(env.calls("pr", "merge")) == 1


def test_land_merges_closes_unmerged_and_reports(env):
    approval = pushed(env)
    env.state["no_auto_merge"] = [3]  # GitHub has not marked PR 3 merged
    env.save()
    state = json.loads(env.state_file("l1").read_text())
    head, heads = state["head"], {rec["pr"]: rec["head"] for rec in state["merged"]}
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    merge = env.git("rev-parse", "refs/heads/main", cwd=env.origin)
    assert res.stdout.splitlines() == [f"MERGED 1 {merge} plangraph:ITEM-A", f"MERGED 2 {merge} plangraph:ITEM-B",
                                       f"MERGED 3 {merge} plangraph:ITEM-C"]
    prs = env.load()["prs"]
    assert (prs["1"]["state"], prs["3"]["state"], prs["3"]["closed_with"]) == ("MERGED", "CLOSED",
                                                                               "merged via batch PR #100")
    runs = [f"Review-Run: plangraph:ITEM-{k} run-part-{n}-0 {heads[n]}" for n, k in ((1, "A"), (2, "B"), (3, "C"))]
    assert env.calls("pr", "merge") == [["pr", "merge", "100", "--repo", "owner/proj", "--merge",
                                         "--match-head-commit", head, "--subject", "batch l1: #1 #2 #3 (#100)",
                                         "--body", "\n".join(["Plan-Item: plangraph:ITEM-A", "Plan-Item: plangraph:ITEM-B",
                                                              "Plan-Item: plangraph:ITEM-C", *runs])]]
    assert env.git("show", "-s", "--format=%s", merge, cwd=env.origin).endswith("(#100)")
    trailers = env.git("log", "-1", "--format=%(trailers:key=Plan-Item,valueonly)", merge, cwd=env.origin)
    assert trailers.split() == ["plangraph:ITEM-A", "plangraph:ITEM-B", "plangraph:ITEM-C"]
    assert env.calls("pr", "checks") == [["pr", "checks", "100", "--repo", "owner/proj", "--required"]]
    assert not env.state_file("l1").exists() and env.git("for-each-ref", "refs/merge-batch") == ""


@pytest.mark.parametrize("subject,ok", [
    ("Custom batch subject", False),
    ("Custom batch #100", False),
    ("Custom batch (#99)", False),
    ("Custom batch (#100) trailing", False),
    ("", False),
    ("(#100)", False),
    ("Custom batch (#100)", True),
])
def test_land_custom_subject_requires_pr_suffix(env, subject, ok):
    approval = pushed(env, prs=(1, 2))
    res = land(env, approval, "--subject", subject)
    if ok:
        assert res.returncode == 0, res.stderr
        merge = env.git("rev-parse", "refs/heads/main", cwd=env.origin)
        assert env.git("show", "-s", "--format=%s", merge, cwd=env.origin) == subject
    else:
        assert res.returncode == 1 and "(#100)" in res.stderr and res.stdout == ""
        assert not env.calls("pr", "merge") and env.state_file("l1").exists()


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
                                       f"MERGED 3 {merge} plangraph:ITEM-C"]
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
                                       f"MERGED 3 {merge} plangraph:ITEM-C"]
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
    assert res.returncode == 1 and "is not in batch head" in res.stderr
    assert not env.calls("pr", "merge")
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


def go_ready(env, *, seats=True, first_files=None, third=False, second_follow_up=False):
    env.branch_pr(1, first_files or {"a.txt": "a\n"}, item="ITEM-A")
    env.branch_pr(2, {"b.txt": "b\n"}, item="ITEM-B")
    if second_follow_up:
        env.push_commit(2, {"b.txt": "b\n", "follow-up.txt": "follow-up\n"})
    if third:
        env.branch_pr(3, {"c.txt": "c\n"}, item="ITEM-C")
    prs = "1,2,3" if third else "1" if first_files is not None else "1,2"
    assert env.run("build", "--name", "g1", "--prs", prs,
                   "--plan", "plangraph:BATCH").returncode == 0
    assert env.run("push", "--name", "g1").returncode == 0
    st = json.loads(env.state_file("g1").read_text())
    for rec in st["merged"]:
        path = env.tmp / f"review-{rec['pr']}.txt"
        path.write_text(f"HEAD: {rec['head']}\nVERDICT: ACCEPT\n")
        assert env.run("review", "--name", "g1", "--pr", str(rec["pr"]), "--file", str(path)).returncode == 0
        if seats:
            env.attach_seat("g1", rec)
    env.load()["runs"] = {"42": {"databaseId": 42, "headSha": st["head"],
                                    "status": "completed", "conclusion": "success"}}
    env.save()
    assert env.run("push", "--name", "g1", "--ci-run", "42").returncode == 0
    return json.loads(env.state_file("g1").read_text())


def test_plain_review_cannot_authorize_go_or_land(env):
    st = go_ready(env, seats=False)
    refused = env.run("go-req", "--name", "g1")
    assert refused.returncode == 1 and "keeper-bound" in refused.stderr
    approval = env.tmp / "approval.txt"
    approval.write_text(f"HEAD: {st['head']}\nVERDICT: PASS\n")
    refused = land(env, approval, name="g1")
    assert refused.returncode == 1 and "keeper-bound" in refused.stderr
    assert not env.calls("pr", "merge")
    for rec in st["merged"]:
        env.attach_seat("g1", rec)
    accepted = env.run("go-req", "--name", "g1")
    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize("field,value", [
    ("role", "implement"), ("item_key", "plangraph:OTHER"),
    ("head_sha", "b" * 40), ("stdout_tail", "TEST_GAMING: no\n"),
    ("stdout_tail", "TEST_GAMING: no\nVERDICT: REQUEST_CHANGES\n"),
    ("stdout_tail", "TEST_GAMING: no\nVERDICT: FIX (LOW: typo)\n"),
    ("file_mode", 0o660),
])
def test_go_req_revalidates_the_keeper_bound_review(env, field, value):
    st = go_ready(env)
    run = next(iter(st["merged"][0]["review_runs"].values()))
    path = env.tmp / "mirror" / "fixture" / run / "record.json"
    if field == "file_mode":
        path.chmod(value)
    else:
        record = json.loads(path.read_text())
        record[field] = value
        path.write_text(json.dumps(record))
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 1 and "keeper-bound" in result.stderr


def test_go_req_from_recorded_evidence(env):
    st = go_ready(env)
    run = next(iter(st["merged"][0]["review_runs"].values()))
    path = env.tmp / "mirror" / "fixture" / run / "record.json"
    record = json.loads(path.read_text())
    record["stdout_tail"] = "TEST_GAMING: no\nVERDICT: ACCEPT\n"
    path.write_text(json.dumps(record))
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1
    # The router readiness grammar requires exactly one explicit request head.
    assert re.findall(r"\b(?:GO-HEAD|head|at)\s+([0-9a-fA-F]{8,40})\b", result.stdout) == [st["head"]]
    assert result.stdout.startswith(f"GO-REQ #100 owner/proj head {st['head']}; diff stat: 2 files changed")
    for rec in st["merged"]:
        run = next(iter(rec["review_runs"].values()))
        assert f"#{rec['pr']} sha={rec['head']} review={run} verdict=ACCEPT" in result.stdout
    assert "GO-KEYS [plangraph:BATCH,plangraph:ITEM-A,plangraph:ITEM-B]" in result.stdout
    assert "CI run 42 state=completed conclusion=success" in result.stdout
    assert result.stdout.endswith("PLAN plangraph:BATCH conforms=yes deviations=none\n")


def test_keeper_migration_needs_a_head_bound_go_marker(env):
    schema = "src/plangraph/keeper/schema_pg.py"
    migrations = "MIGRATIONS = (Migration(1, 'baseline', DDL),{} )\nLATEST = MIGRATIONS[-1].version\n"
    env.seed({schema: migrations.format("")})
    st = go_ready(env, first_files={schema: migrations.format(" Migration(2, 'add table', NEW_DDL),")})
    head = st["head"]
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert "; MIGRATION 2;" in result.stdout

    approval = env.tmp / "approval.txt"
    approval.write_text(f"HEAD: {head}\nVERDICT: PASS\n")
    env.load()["checks"]["100"] = [{"name": "ci-gate", "bucket": "pass"}]
    env.save()
    refused = land(env, approval, name="g1")
    assert refused.returncode == 1 and "must include MIGRATION 2" in refused.stderr
    assert not env.calls("pr", "merge")
    row = env.tmp / "go-row.txt"
    for text in (
        f"GO owner/proj PR #100 GO-HEAD {head}; MIGRATION 1",
        f"GO owner/proj PR #100 GO-HEAD {'f' * 40}; MIGRATION 2",
        f"NO GO owner/proj PR #100 GO-HEAD {head}; MIGRATION 2",
        f"GO owner/proj PR #100 GO-HEAD {head}\nMIGRATION 2",
    ):
        row.write_text(text)
        refused = land(env, approval, "--go-row", str(row), name="g1")
        assert refused.returncode == 1 and f"MIGRATION 2" in refused.stderr
        assert not env.calls("pr", "merge")
    row.write_text(f"| D-123 | proj | (15:46Z, date -u) GO #100 proj at GO-HEAD {head}; MIGRATION 2 | ROUTER |\n")
    accepted = land(env, approval, "--go-row", str(row), name="g1")
    assert accepted.returncode == 0, accepted.stderr
    assert len(env.calls("pr", "merge")) == 1


def test_land_refuses_legacy_batch_without_a_saved_base(env):
    approval = pushed(env, prs=(1, 2))
    path = env.state_file("l1")
    state = json.loads(path.read_text())
    del state["base_head"]
    path.write_text(json.dumps(state))
    refused = land(env, approval)
    assert refused.returncode == 1
    assert "no saved base head" in refused.stderr
    assert "Traceback" not in refused.stderr
    assert not env.calls("pr", "merge")


@pytest.mark.parametrize("case", ["missing", "wrong_head", "reject", "deleted", "changed"])
def test_go_req_refuses_missing_or_stale_part_accept(env, case):
    st = go_ready(env)
    rec = st["merged"][1]
    path = Path(rec["review"]["file"])
    if case == "missing":
        del rec["review_runs"]
    elif case == "wrong_head":
        rec["review"]["head"] = st["head"]
    elif case == "reject":
        rec["review"]["verdict"] = "REQUEST_CHANGES"
    elif case == "deleted":
        path.unlink()
    else:
        path.write_text(f"HEAD: {rec['head']}\nVERDICT: REQUEST_CHANGES\n")
    env.state_file("g1").write_text(json.dumps(st))
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 1 and result.stdout == ""


@pytest.mark.parametrize("field,value", [("status", "in_progress"), ("conclusion", "failure"),
                                          ("headSha", "a" * 40)])
def test_go_req_refuses_ci_not_green_on_batch_head(env, field, value):
    go_ready(env)
    env.load()["runs"]["42"][field] = value
    env.save()
    pushed = env.run("push", "--name", "g1", "--ci-run", "42")
    assert pushed.returncode == (1 if field == "headSha" else 0)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 1 and "CI is not green" in result.stderr and result.stdout == ""


def test_go_req_new_head_invalidates_ci_and_reports_deviations(env):
    st = go_ready(env)
    wt = st["worktree"]
    env.git("commit", "--allow-empty", "-qm", "batch fix", cwd=wt)
    assert "head moved" in env.run("go-req", "--name", "g1").stderr
    assert env.run("push", "--name", "g1").returncode == 0
    assert json.loads(env.state_file("g1").read_text())["ci"] is None
    env.load()["runs"]["42"]["headSha"] = env.git("rev-parse", "HEAD", cwd=wt)
    env.save()
    assert env.run("push", "--name", "g1", "--ci-run", "42").returncode == 0
    st = json.loads(env.state_file("g1").read_text())
    st["deviations"] = "operator approved scope change"
    env.state_file("g1").write_text(json.dumps(st))
    assert env.run("go-req", "--name", "g1").stdout.endswith(
        "PLAN plangraph:BATCH conforms=no deviations=operator approved scope change\n")


def test_review_rejection_supersedes_old_accept(env):
    st = go_ready(env)
    path = env.tmp / "rejection.txt"
    path.write_text(f"HEAD: {st['merged'][0]['head']}\nVERDICT: REQUEST_CHANGES\n")
    result = env.run("review", "--name", "g1", "--pr", "1", "--file", str(path))
    assert result.returncode == 0, result.stderr
    rec = json.loads(env.state_file("g1").read_text())["merged"][0]
    assert rec["review"]["verdict"] == "REQUEST_CHANGES" and rec["review"]["file"] == str(path)
    assert env.run("go-req", "--name", "g1").returncode == 1


def test_go_req_refuses_remote_head_movement(env):
    st = go_ready(env)
    env.git("update-ref", "refs/heads/batch/g1", st["base_head"], cwd=env.origin)
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 1 and "saved pushed head" in result.stderr and result.stdout == ""


@pytest.mark.parametrize("exit_code", [0, 7])
def test_land_sweep_targets_only_landed_parts_and_failure_is_nonfatal(env, exit_code):
    approval = pushed(env)
    saved = json.loads(env.state_file("l1").read_text())
    detached = env.tmp / "detached-part"
    env.git("worktree", "add", "--detach", str(detached), saved["prs"]["1"]["head"])
    part_branch = env.tmp / "part-branch"
    env.git("worktree", "add", str(part_branch), saved["prs"]["3"]["ref"])
    unrelated = Path(saved["worktree"]).parent / "unrelated"
    env.git("worktree", "add", "--detach", str(unrelated), "main")
    env.push_commit(2, {"b.txt": "moved\n"})
    log = env.tmp / "sweep.json"
    sweep = env.stubs / "pg-worktree-sweep"
    sweep.write_text("#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
                     f"with Path({str(log)!r}).open('a') as f: print(json.dumps(sys.argv[1:]), file=f)\n"
                     f"sys.exit({exit_code})\n")
    sweep.chmod(0o755)
    result = land(env, approval)
    assert result.returncode == 0
    assert ("cleanup sweep failed" in result.stderr) == bool(exit_code)
    invocation, = log.read_text().splitlines()
    args = json.loads(invocation)
    assert "--apply" in args and "--delete-remote" in args
    assert args.count("--include-merged") == 1
    assert args[args.index("--target") + 1] == (
        f"{saved['worktree']}:refs/heads/{saved['branch']}:{saved['head']}")
    targets = [args[i + 1] for i, arg in enumerate(args) if arg == "--target"]
    assert targets == [f"{saved['worktree']}:refs/heads/{saved['branch']}:{saved['head']}",
                       f"{detached}:-:{saved['prs']['1']['head']}",
                       f"{part_branch}:refs/heads/{saved['prs']['3']['ref']}:{saved['prs']['3']['head']}"]
    assert all(str(unrelated) not in target for target in targets)
    assert unrelated.exists()
    remote = [args[i + 1] for i, arg in enumerate(args) if arg == "--remote-target"]
    assert remote == [f"{saved['prs'][str(n)]['ref']}:{saved['prs'][str(n)]['head']}" for n in (1, 3)]


def test_land_cleanup_metadata_failure_is_nonfatal(env):
    approval = pushed(env, prs=(1, 2))
    saved = json.loads(env.state_file("l1").read_text())
    del saved["prs"]["1"]["ref"]
    env.state_file("l1").write_text(json.dumps(saved))
    result = land(env, approval)
    assert result.returncode == 0 and "cleanup failed:" in result.stderr
    assert result.stdout.count("MERGED") == 2


def test_land_names_the_pending_check_and_does_not_retry(env):
    approval = pushed(env, prs=(1, 2))
    env.state["checks"]["100"] = [{"name": "ci-gate", "bucket": "pending"}]
    env.save()
    res = land(env, approval)
    assert res.returncode == 1 and "pending: ci-gate" in res.stderr and "does not wait or retry" in res.stderr
    assert not env.calls("pr", "merge")


def test_land_refuses_a_head_green_on_an_older_main(env):
    """LE-MAIN-RED-AFTER-GREEN-MERGES: a green run of a head that lacks main's tip does not merge."""
    approval = pushed(env, prs=(1, 2))
    env.state["behind_by"] = 2
    env.save()
    res = land(env, approval)
    assert res.returncode == 1 and res.stdout == "", res.stderr
    assert "stale_base" in res.stderr and "2 commit(s) behind" in res.stderr
    assert not env.calls("pr", "merge") and env.state_file("l1").exists()
    env.state["behind_by"] = 0
    env.save()
    assert land(env, approval).returncode == 0


def _merge_close_stub(env, exit_code):
    """A `plangraph` on the hermetic PATH that records its argv and prints a report."""
    stub = env.stubs / "plangraph"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{env.tmp}/merge-close.args"\n'
                    f'echo "listed demo:X in_review forbidden"\nexit {exit_code}\n')
    stub.chmod(0o755)
    return env.tmp / "merge-close.args"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_land_runs_the_merge_close_job_once_and_never_fails_on_it(env, exit_code):
    args = _merge_close_stub(env, exit_code)
    res = land(env, pushed(env))
    assert res.returncode == 0, res.stderr
    assert args.read_text() == "merge-close\n"
    assert "listed demo:X in_review forbidden" in res.stderr
    assert "listed" not in res.stdout


def test_land_without_the_job_binary_still_lands(env):
    res = land(env, pushed(env))
    assert res.returncode == 0 and "merge-close not run" in res.stderr


def test_land_names_the_batch_item_in_the_merge_commit(env):
    approval = pushed(env)
    state = json.loads(env.state_file("l1").read_text())
    state["plan"] = "plangraph:BATCH-1"
    env.state_file("l1").write_text(json.dumps(state))
    assert land(env, approval).returncode == 0
    merge = env.git("rev-parse", "refs/heads/main", cwd=env.origin)
    trailers = env.git("log", "-1", "--format=%(trailers:key=Plan-Item,valueonly)", merge, cwd=env.origin)
    assert trailers.split() == ["plangraph:BATCH-1", "plangraph:ITEM-A", "plangraph:ITEM-B", "plangraph:ITEM-C"]


def test_land_reads_main_last_right_before_the_merge_call():
    """The admission read follows the rerun-comment work, so main turning red during it still stops the merge."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "tools" / "merge_batch_land.py").read_text(encoding="utf-8")
    src = text[text.index("def cmd_land("):]
    assert src.index("record_reruns(") < src.index("require_main_green(") < src.index('"pr", "merge"')


@pytest.mark.parametrize("target_kind", ["inner", "short-merge"])
def test_go_req_copies_the_train_part_keys_without_dropped_or_skipped_parts(env, target_kind):
    st = go_ready(env, third=True, second_follow_up=target_kind == "inner")
    wt = Path(st["worktree"])
    dropped = st["merged"][1]
    if target_kind == "inner":
        target = env.git("rev-parse", f"{dropped['head']}^", cwd=wt)
        env.git("revert", "--no-commit", target, cwd=wt)
        trailers = "\n\nDropped-Part: plangraph:ITEM-B"
    else:
        target, trailers = dropped["merge"][:12], ""
        env.git("revert", "-m", "1", "--no-commit", target, cwd=wt)
    env.git("commit", "-qm", f"Train 9: drop b2\n\nThis reverts commit {target}.{trailers}", cwd=wt)
    st["skipped"].append({"pr": 4, "head": "a" * 40, "files": [], "reason": "fixture skip",
                          "items": ["plangraph:ITEM-D"]})
    env.state_file("g1").write_text(json.dumps(st))
    env.load()["runs"]["42"]["headSha"] = env.git("rev-parse", "HEAD", cwd=wt)
    env.save()
    assert env.run("push", "--name", "g1", "--ci-run", "42").returncode == 0

    block = "GO-KEYS [plangraph:BATCH,plangraph:ITEM-A,plangraph:ITEM-C]"
    assert block in env.load()["prs"]["100"]["body"]
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0 and block in result.stdout
    assert "#2 sha=" not in result.stdout and "#4 sha=" not in result.stdout


def test_go_req_key_list_is_generated_from_the_train_trailers(env):
    """A resolver commit's key is named, a key trailered only on a checkpoint doc is deferred, not delivered."""
    st = go_ready(env)
    wt = Path(st["worktree"])
    (wt / "resolver.txt").write_text("fix\n")
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", "resolver slice\n\nPlan-Item: plangraph:RESOLVER-KEY", cwd=wt)
    (wt / "docs/checkpoints").mkdir(parents=True)
    (wt / "docs/checkpoints/cp.md").write_text("deferred\n")
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", "checkpoint\n\nPlan-Item: plangraph:DEFERRED-KEY", cwd=wt)
    env.load()["runs"]["42"]["headSha"] = env.git("rev-parse", "HEAD", cwd=wt)
    env.save()
    assert env.run("push", "--name", "g1", "--ci-run", "42").returncode == 0
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert ("GO-KEYS [plangraph:BATCH,plangraph:ITEM-A,plangraph:ITEM-B,plangraph:RESOLVER-KEY]; "
            "GO-DEFERRED [plangraph:DEFERRED-KEY];") in result.stdout


def test_go_req_key_list_works_for_a_land_state_without_a_base_name(env):
    """Older land states carry no ``base``: the default branch is main, as the rest of the landing reads it."""
    st = go_ready(env)
    st.pop("base")
    env.state_file("g1").write_text(json.dumps(st))
    result = env.run("go-req", "--name", "g1")
    assert result.returncode == 0, result.stderr
    assert "GO-KEYS [plangraph:BATCH,plangraph:ITEM-A,plangraph:ITEM-B]" in result.stdout


def test_push_renders_the_pr_body_key_list_at_the_new_head(env):
    """A resolver commit added before a second push is in the PR body of that push, not the next one."""
    st = go_ready(env)
    wt = Path(st["worktree"])
    (wt / "resolver.txt").write_text("fix\n")
    env.git("add", "-A", cwd=wt)
    env.git("commit", "-qm", "resolver slice\n\nPlan-Item: plangraph:RESOLVER-KEY", cwd=wt)
    assert env.run("push", "--name", "g1").returncode == 0
    assert "GO-KEYS [plangraph:BATCH,plangraph:ITEM-A,plangraph:ITEM-B,plangraph:RESOLVER-KEY]" in env.load()["prs"]["100"]["body"]
