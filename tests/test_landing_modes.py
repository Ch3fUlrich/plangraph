"""LAND-MODE-TESTED-FF and LAND-MODE-SETTING: `merge_batch.py land` in each landing mode.

HERMETIC like tests/test_merge_batch_land.py: a temp bare origin plus clone, a stub gh, the tool as a
subprocess; the origin is a real Git repository, so a base-conditional push is really applied or refused.

Mutations killed: tested-ff that makes a merge commit or calls `gh pr merge`; a push without the lease on the
tested base; a lease that lets main moved into a commit the train contains pass; a lost answer that is not
settled by reading main, or that pushes twice after it landed; a server refusal retried or reported as
landed; train-pr-merge that no longer merges; single-pr that lands a train of several parts; an unknown mode
that lands; a landing with no mode in its receipt or telemetry row; a changed setting that changes nothing.
"""
import json
import sqlite3
import sys

import pytest

from merge_batch_env import env_fixture, head_of, land, pushed

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_LANDING_DB", str(tmp_path / "landing.sqlite3"))
    for built in env_fixture(tmp_path):
        built.env["PLANGRAPH_LANDING_DB"] = str(tmp_path / "landing.sqlite3")
        built.env["PLANGRAPH_SETTINGS"] = "plangraph.json"
        built.env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
        yield built


def mode(env, value, **table):
    (env.repo / "plangraph.json").write_text(json.dumps({"landing": {"mode": value, **table}}))


def main_tip(env):
    return env.git("rev-parse", "refs/heads/main", cwd=env.origin)


def rows(env):
    with sqlite3.connect(env.tmp / "landing.sqlite3") as db:
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute("SELECT * FROM landing")]
        except sqlite3.OperationalError:  # no landing ever wrote the table
            return []


def test_tested_ff_moves_main_to_the_train_head_without_a_merge_commit(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    head = head_of(env)
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    assert main_tip(env) == head
    assert env.calls("pr", "merge") == []
    assert res.stdout.splitlines() == [f"MERGED 1 {head} plangraph:ITEM-A", f"MERGED 2 {head} plangraph:ITEM-B",
                                       f"MERGED 3 {head} plangraph:ITEM-C"]
    assert not env.state_file("l1").exists()


def test_tested_ff_pushes_with_a_lease_on_the_tested_base(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    base = json.loads(env.state_file("l1").read_text())["base_head"]
    head = head_of(env)
    log = env.tmp / "push.log"
    hook = env.origin / "hooks" / "pre-receive"
    hook.write_text(f'#!/bin/sh\nwhile read old new ref; do echo "$ref $old $new" >> {log}; done\n')
    hook.chmod(0o755)
    assert land(env, approval).returncode == 0
    assert log.read_text().split() == ["refs/heads/main", base, head]


def test_tested_ff_refuses_when_main_moved_into_a_commit_the_train_contains(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    state = json.loads(env.state_file("l1").read_text())
    inside = env.git("rev-parse", f"{state['head']}~1", cwd=env.repo)  # a commit of the train, not its base
    env.git("push", "-q", "origin", f"{inside}:refs/heads/main")
    res = land(env, approval)
    assert res.returncode == 1 and "base_moved" in res.stderr, res.stderr
    assert main_tip(env) == inside and env.state_file("l1").exists()
    assert rows(env) == []


def test_tested_ff_settles_a_lost_answer_by_reading_main_and_pushes_once(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    head = head_of(env)
    res = env.run("land", "--name", "l1", "--batch-pr", "100", "--approval", str(approval), "--wait", "0",
                  crash=("push", 1, "fail"))
    assert res.returncode == 0, res.stderr
    assert main_tip(env) == head and (env.tmp / "crash.count").read_text().strip() == "1"
    assert [r["mode"] for r in rows(env)] == ["tested-ff"]


def test_tested_ff_sends_the_same_push_again_while_main_is_still_the_base(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    head = head_of(env)
    res = env.run("land", "--name", "l1", "--batch-pr", "100", "--approval", str(approval), "--wait", "0",
                  crash=("push", 1, "refuse"))
    assert res.returncode == 0, res.stderr
    assert main_tip(env) == head and (env.tmp / "crash.count").read_text().strip() == "2"


def test_tested_ff_does_not_retry_a_refusal_by_the_server(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    base = main_tip(env)
    hook = env.origin / "hooks" / "pre-receive"
    hook.write_text('#!/bin/sh\necho "protected branch: required status check land-auth is missing" >&2\nexit 1\n')
    hook.chmod(0o755)
    res = env.run("land", "--name", "l1", "--batch-pr", "100", "--approval", str(approval), "--wait", "0",
                  crash=("push", 99, "fail"))
    assert res.returncode == 1 and "refused" in res.stderr and "land-auth" in res.stderr, res.stderr
    assert (env.tmp / "crash.count").read_text().strip() == "1"  # one push, no retry
    assert main_tip(env) == base and env.state_file("l1").exists() and rows(env) == []


def test_tested_ff_is_found_by_ancestry_when_a_rerun_finds_main_already_at_the_head(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    env.git("push", "-q", "origin", f"{head_of(env)}:refs/heads/main")  # an earlier land whose answer was lost
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    assert env.calls("pr", "merge") == [] and [r["mode"] for r in rows(env)] == ["tested-ff"]


def test_train_pr_merge_still_merges_the_pull_request_with_a_merge_commit(env):
    approval = pushed(env)
    head = head_of(env)
    mode(env, "train-pr-merge")
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    assert len(env.calls("pr", "merge")) == 1 and main_tip(env) != head
    assert [r["mode"] for r in rows(env)] == ["train-pr-merge"]


def test_single_pr_lands_one_pull_request_and_refuses_a_train(env):
    approval = pushed(env, prs=(1,))
    mode(env, "single-pr")
    assert land(env, approval).returncode == 0
    assert [(r["mode"], r["parts"]) for r in rows(env)] == [("single-pr", "[1]")]


def test_single_pr_refuses_a_train_of_several_parts(env):
    approval = pushed(env)
    mode(env, "single-pr")
    res = land(env, approval)
    assert res.returncode == 1 and "single-pr" in res.stderr and "3 parts" in res.stderr
    assert env.calls("pr", "merge") == [] and env.state_file("l1").exists()


def test_an_unknown_mode_is_refused_before_anything_is_asked_of_github(env):
    approval = pushed(env)
    mode(env, "fast-forward")
    before = len(env.calls())
    res = land(env, approval)
    assert res.returncode == 1 and "landing" in res.stderr and "tested-ff" in res.stderr
    assert len(env.calls()) == before and main_tip(env) != head_of(env)


def test_changing_the_setting_changes_the_path_without_a_code_change(env):
    approval = pushed(env)
    head = head_of(env)
    mode(env, "train-pr-merge", repos={"owner/proj": "tested-ff"})  # the repository entry wins
    assert land(env, approval).returncode == 0
    assert env.calls("pr", "merge") == [] and main_tip(env) == head


@pytest.mark.parametrize("value", ["tested-ff", "train-pr-merge"])
def test_the_receipt_and_the_row_name_the_mode_and_the_telemetry_columns(env, value):
    approval = pushed(env)
    mode(env, value)
    env.state["events"] = {"1": [{"event": "labeled", "label": {"name": "batch-ready"},
                                  "created_at": "2026-11-21T08:00:00Z"}]}
    env.save()
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    (row,) = rows(env)
    assert (row["mode"], row["repo"], row["train"], row["pr"], row["parts"]) == (value, "owner/proj", "l1", 100, "[1, 2, 3]")
    assert row["landed_sha"] == main_tip(env) and row["first_ready_at"] == "2026-11-21T08:00:00Z"
    assert row["ci_run_id"] == 7 and row["ci_started_at"] and row["ci_ended_at"] and row["landed_at"]
    receipt = json.loads((env.repo / ".git" / "merge-batch" / "receipts" / f"l1-{row['landed_sha']}.json").read_text())
    assert (receipt["mode"], receipt["train"], receipt["landed_sha"]) == (value, "l1", row["landed_sha"])
    assert (row["green_at"] is not None) == (value == "tested-ff")  # the green run is the tested run itself


def test_a_landing_whose_row_cannot_be_written_fails_the_lander(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    blocker = env.tmp / "blocked"
    blocker.write_text("a file, not a directory")
    env.env["PLANGRAPH_LANDING_DB"] = str(blocker / "landing.sqlite3")
    res = land(env, approval)
    assert res.returncode == 1 and "telemetry" in res.stderr


def test_landing_report_and_settle_commands(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    assert land(env, approval).returncode == 0
    res = env.run("landing-report")
    assert res.returncode == 0 and "tested-ff" in res.stdout and "trains" in res.stdout, res.stderr
    assert json.loads(env.run("landing-report", "--json").stdout)[0]["mode"] == "tested-ff"
    assert env.run("landing-mode", "--repo", "owner/proj").stdout == "tested-ff\n"
