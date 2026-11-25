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
import shutil
import sqlite3
import sys

import pytest

from merge_batch_env import env_fixture, head_of, land, pushed
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.store import Store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_LANDING_DB", str(tmp_path / "landing.sqlite3"))
    for built in env_fixture(tmp_path):
        built.env["PLANGRAPH_LANDING_DB"] = str(tmp_path / "landing.sqlite3")
        built.env["PLANGRAPH_SETTINGS"] = "plangraph.json"
        built.env["PG_MERGE_CLOSE_REQUIRE_SEATS"] = "off"  # tested-ff needs the explicit acknowledgement
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


@pytest.fixture
def journal(env):
    path = env.tmp / "keeper.db"
    db = open_db(path, create=True, backend="sqlite")
    add_principal(db, "lander", "router", [])
    db.close()
    env.env.update(PLANGRAPH_ENV_FILE=str(env.tmp / "keeper.env"), JOURNAL_DB=str(path),
                   JOURNAL_SWEEP_LOG=str(env.tmp / "sweep"), JOURNAL_LANE_LOG=str(env.tmp / "lane"),
                   PG_LANE_STATE=str(env.tmp / "lanestate"))
    binary = env.stubs / "plangraph"
    binary.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
from plangraph.keeper.auth import Principal
from plangraph.keeper.db import open_db
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.keeper.store import Store

args = sys.argv[1:]
if args == ['landing', 'journal']:
    body = json.load(sys.stdin)
    if body['action'] == 'prepare':
        main = subprocess.check_output(['git', 'ls-remote', 'origin', 'refs/heads/main'],
                                       text=True).split()[0]
        assert main == body['base'], 'the journal must be prepared before main moves'
        if os.environ.get('LEGACY_LANDING'):
            body.pop('mode', None)
            body.pop('receipt_inputs', None)
    if body['action'] == 'landed' and os.environ.get('INTERRUPT_LANDING'):
        sys.exit('interrupted before saving the part outcomes')
    db = open_db(os.environ['JOURNAL_DB'], backend='sqlite')
    cfg = Path.home() / '.config/plangraph/keeper.json'
    mirror = json.loads(cfg.read_text()).get('spawner_records_dir') if cfg.is_file() else None
    store = Store(db, ctx={'spawner_records': SpawnerRecordAdapter(mirror)})
    print(json.dumps(store.land(Principal('lander', 'router', []), body)))
    db.close()
elif args[:2] == ['landing', 'verify-hosts']:
    head = args[-1]
    rows = [dict(host=str(n), head=head, checkout=head, hooks=head, build=head, synced=False) for n in range(3)]
    if os.environ.get('WRITER_REPORT'):  # every host reports its writer-stop result
        rows = [dict(row, writers=[dict(status='stopped', run_id='r%s' % row['host'])]) for row in rows]
    print(json.dumps(dict(verified=rows)))
elif args[:1] == ['lane-cleanup']:
    with open(os.environ['JOURNAL_LANE_LOG'], 'a') as log:
        print(json.dumps(args), file=log)
else:
    assert args[:2] == ['landing', 'ci-import'] or args == ['merge-close'], args
""")
    binary.chmod(0o755)
    sweep = env.stubs / "pg-worktree-sweep"
    sweep.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$JOURNAL_SWEEP_LOG"\n')
    sweep.chmod(0o755)

    def request(action="show", **fields):
        db = open_db(path, backend="sqlite")
        try:
            return Store(db).land(Principal("lander", "router", []),
                                  dict(repo=env.state["repo"], pr=100, action=action, **fields))
        finally:
            db.close()

    return request


@pytest.mark.parametrize("operation", ["push", "review", "land"])
def test_active_parts_agree_after_a_drop(env, journal, operation):
    approval = pushed(env, prs=(1, 2), plan="plangraph:BATCH")
    st = json.loads(env.state_file("l1").read_text())
    worktree = st["worktree"]
    dropped = st["merged"][1]
    env.git("revert", "-m", "1", "--no-edit", dropped["merge"], cwd=worktree)
    head = env.git("rev-parse", "HEAD", cwd=worktree)
    result = env.run("push", "--name", "l1")
    assert result.returncode == 0, result.stderr
    body = env.load()["prs"]["100"]["body"]
    assert "| #1 |" in body and "| #2 |" not in body

    if operation == "review":
        env.attach_seat("l1", st["merged"][0])
        refused = env.run("review", "--name", "l1", "--pr", str(dropped["pr"]),
                          "--run-id", next(iter(dropped["review_runs"].values())))
        assert refused.returncode == 1 and "not an included part" in refused.stderr
    if operation == "land":
        mode(env, "single-pr")
        approval.write_text(f"HEAD: {head}\nVERDICT: PASS\n")
        result = land(env, approval)
        assert result.returncode == 0, result.stderr
    train = journal()["train"]
    assert [part["pr"] for part in train["parts"]] == [1]
    if operation == "land":
        assert train["included"] == [1]
    else:
        assert train["parts"][0]["review_runs"]


@pytest.mark.parametrize("keeper_configured", [False, True])
def test_tested_ff_moves_main_to_the_train_head_without_a_merge_commit(env, keeper_configured, request):
    approval = pushed(env)
    if keeper_configured:
        journal = request.getfixturevalue("journal")
    mode(env, "tested-ff")
    head = head_of(env)
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    assert main_tip(env) == head
    assert env.calls("pr", "merge") == []
    assert res.stdout.splitlines() == [f"MERGED 1 {head} plangraph:ITEM-A", f"MERGED 2 {head} plangraph:ITEM-B",
                                       f"MERGED 3 {head} plangraph:ITEM-C"]
    assert not env.state_file("l1").exists()
    if keeper_configured:
        train = journal()["train"]
        assert train["mode"] == "tested-ff" and train["merge"] == head
        assert train["included"] == [1, 2, 3] and train["open_steps"] == []
        assert len(train["host_verification"]) == 3
        frozen = [{"pr": part["pr"], "head": part["head"]} for part in train["parts"]]
        assert journal("consumed", parts=frozen)["consumed"] == frozen


@pytest.mark.parametrize("reported", [False, True])
def test_settlement_runs_the_lane_verb_only_after_a_recorded_writer_stop_report(env, journal, reported):
    """merge_batch.py land/settle: host verification without a writer report holds the lane refs."""
    approval = pushed(env)
    mode(env, "tested-ff")
    if reported:
        env.env["WRITER_REPORT"] = "1"
    res = land(env, approval)
    assert res.returncode == 0, res.stderr
    train = journal()["train"]
    assert train["open_steps"] == [] and all(("writers" in host) == reported for host in train["host_verification"])
    sweep = (env.tmp / "sweep").read_text().splitlines()
    lane_remote = {s for s in sweep if s.startswith("lane/")}
    frozen = {f"{part['ref']}:{part['head']}" for part in train["parts"] if part["pr"] in train["included"]}
    holds = [row.split("\t")[0] for row in (env.tmp / "lanestate/lane-holds.txt").read_text().splitlines()]
    if reported:
        assert not [hold for hold in holds if hold.removeprefix("refs/heads/") in {p["ref"] for p in train["parts"]}]
        (call,) = [json.loads(line) for line in (env.tmp / "lane").read_text().splitlines()]
        refs = call[call.index("--refs") + 1].split(",")
        assert set(refs) == {f"refs/heads/{pair}" for pair in frozen} and "--apply" in call
        assert lane_remote == frozen
    else:  # held from before the merge until a writer-stop report is recorded
        assert {f"refs/heads/{p['ref']}" for p in train["parts"] if p["pr"] in train["included"]} <= set(holds)
        assert not (env.tmp / "lane").exists() and lane_remote == set()
        assert "--delete-remote" not in sweep


@pytest.mark.parametrize("local_state", [True, False])
def test_tested_ff_recovers_after_push_before_part_outcomes_are_saved(env, request, local_state):
    approval = pushed(env)
    journal = request.getfixturevalue("journal")
    mode(env, "tested-ff")
    env.env["WRITER_REPORT"] = "1"  # the hosts report their writer-stop result, so landed lane refs may go
    head = head_of(env)
    env.env["INTERRUPT_LANDING"] = "1"
    interrupted = land(env, approval)
    del env.env["INTERRUPT_LANDING"]
    assert interrupted.returncode != 0 and "interrupted before saving" in interrupted.stderr
    assert main_tip(env) == head
    train = journal()["train"]
    assert train["merge"] is None and "included" not in train
    if not local_state:
        env.state_file("l1").unlink()

    env.push_commit(2, {"b.txt": "late change"})
    env.git("fetch", "-q", "origin")
    env.git("merge", "--ff-only", "origin/main")
    env.seed({"later.txt": "main moved forward"})
    assert main_tip(env) != head
    resumed = env.run("settle")
    assert resumed.returncode == 0, resumed.stderr
    lines = resumed.stdout.splitlines()
    assert "NOT-MERGED 2 head moved" in lines
    assert not any(line.startswith("MERGED 2 ") for line in lines)
    train = journal()["train"]
    assert train["merge"] == head and train["included"] == [1, 3]
    assert train["open_steps"] == [] and len(train["host_verification"]) == 3
    (row,) = rows(env)
    assert (row["mode"], row["train"], row["landed_sha"]) == ("tested-ff", "l1", head)
    receipt = json.loads((env.state_file("l1").parent / "receipts" / f"l1-{head}.json").read_text())
    assert receipt["mode"] == "tested-ff" and receipt["review_runs"]["1"]
    sweep = (env.tmp / "sweep").read_text().splitlines()
    assert {s for s in sweep if s.startswith("lane/")} == {
        f"{part['ref']}:{part['head']}" for part in train["parts"] if part["pr"] in (1, 3)
    }


def test_tested_ff_settlement_waits_for_seat_acknowledgement(env, journal):
    approval = pushed(env)
    mode(env, "tested-ff")
    env.env["INTERRUPT_LANDING"] = "1"
    assert "interrupted before saving" in land(env, approval).stderr
    del env.env["INTERRUPT_LANDING"]
    del env.env["PG_MERGE_CLOSE_REQUIRE_SEATS"]

    resumed = env.run("settle")
    assert resumed.returncode == 1 and "PG_MERGE_CLOSE_REQUIRE_SEATS" in resumed.stderr, resumed.stderr
    assert journal()["train"]["open_steps"] == ["close_parts", "host_sync", "cleanup"]

    env.env["PG_MERGE_CLOSE_REQUIRE_SEATS"] = "off"
    resumed = env.run("settle")
    assert resumed.returncode == 0, resumed.stderr
    assert journal()["train"]["open_steps"] == []


def test_legacy_single_pr_settlement_uses_matching_saved_mode(env, journal):
    approval = pushed(env, prs=(1,))
    mode(env, "single-pr")
    env.env.update(LEGACY_LANDING="1", INTERRUPT_LANDING="1")
    interrupted = land(env, approval)
    del env.env["INTERRUPT_LANDING"]
    assert "interrupted before saving" in interrupted.stderr, interrupted.stderr
    mode(env, "train-pr-merge")

    resumed = env.run("settle")
    assert resumed.returncode == 0, resumed.stderr
    (row,) = rows(env)
    receipt = json.loads((env.state_file("l1").parent / "receipts" / f"l1-{main_tip(env)}.json").read_text())
    assert row["mode"] == receipt["mode"] == "single-pr"


def test_tested_ff_settlement_abandons_a_closed_unlanded_pr(env, journal):
    approval = pushed(env)
    mode(env, "tested-ff")
    base = main_tip(env)
    hook = env.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert "refused" in land(env, approval).stderr
    assert journal()["train"]["merge"] is None
    assert env.run_gh("pr", "close", "100").returncode == 0
    recovery = env.fresh_clone()
    assert not env.has_commit(recovery, head_of(env))

    resumed = env.run("settle", cwd=recovery)
    assert resumed.returncode == 0, resumed.stderr
    assert journal()["train"]["abandoned"]
    assert main_tip(env) == base and rows(env) == []


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


@pytest.mark.parametrize("where", ["inside", "unrelated"])
def test_tested_ff_lease_refuses_a_main_that_moves_between_the_read_and_the_push(env, where):
    """A git that moves origin's main right before the push: the lease on the tested base refuses it, also when
    main moved into a commit the batch contains (which the head could still fast-forward from)."""
    approval = pushed(env)
    mode(env, "tested-ff")
    state = json.loads(env.state_file("l1").read_text())
    if where == "inside":
        target = env.git("rev-parse", f"{state['head']}~1", cwd=env.repo)
    else:
        env.git("checkout", "-q", "-b", "other", state["base_head"], cwd=env.repo)
        (env.repo / "other.txt").write_text("other\n")
        env.git("add", "other.txt", cwd=env.repo)
        env.git("commit", "-q", "-m", "someone else landed", cwd=env.repo)
        target = env.git("rev-parse", "HEAD", cwd=env.repo)
        env.git("checkout", "-q", "main", cwd=env.repo)
    real = shutil.which("git")
    wrap = env.tmp / "wrap"
    wrap.mkdir()
    (wrap / "git").write_text(f'#!/bin/sh\nfor a in "$@"; do if [ "$a" = push ]; then '
                              f'{real} --git-dir {env.origin} fetch -q {env.repo} {target}:refs/heads/moved && '
                              f'{real} --git-dir {env.origin} update-ref refs/heads/main {target}; fi; done\nexec {real} "$@"\n')
    (wrap / "git").chmod(0o755)
    env.env["PATH"] = f"{wrap}:{env.env['PATH']}"
    res = land(env, approval)
    assert res.returncode == 1 and "base_moved" in res.stderr, res.stderr
    assert main_tip(env) == target and env.state_file("l1").exists() and rows(env) == []


def test_a_merged_pull_request_is_no_tested_ff_landing_unless_its_head_is_in_main(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    env.load()
    env.state["prs"]["100"].update(state="MERGED", mergeCommit={"oid": "c" * 40})  # merged another way (squash)
    env.save()
    res = land(env, approval)
    assert res.returncode == 1 and "merged another way" in res.stderr, res.stderr
    assert rows(env) == [] and env.state_file("l1").exists()


def test_landing_record_writes_the_row_for_a_landing_made_by_another_tool(env):
    res = env.run("landing-record", "--repo", "owner/proj", "--mode", "tested-ff", "--train", "pr9", "--pr", "9",
                  "--sha", "a" * 40, "--parts", "9")
    assert res.returncode == 0, res.stderr
    (row,) = rows(env)
    assert (row["mode"], row["train"], row["landed_sha"], row["parts"]) == ("tested-ff", "pr9", "a" * 40, "[9]")
    blocker = env.tmp / "blocked"
    blocker.write_text("a file")
    env.env["PLANGRAPH_LANDING_DB"] = str(blocker / "x.sqlite3")
    res = env.run("landing-record", "--repo", "owner/proj", "--mode", "tested-ff", "--train", "pr9", "--pr", "9",
                  "--sha", "a" * 40, "--parts", "9")
    assert res.returncode == 1 and "telemetry row not written" in res.stderr


def test_build_writes_the_batch_plan_key_into_every_merge_commit_it_makes(env):
    pushed(env, plan="plangraph:BATCH-X")
    state = json.loads(env.state_file("l1").read_text())
    trailers = env.git("log", "--merges", "--format=%(trailers:key=Plan-Item,valueonly)", f"{state['base_head']}..{state['head']}",
                       cwd=env.repo)
    assert trailers.split() == ["plangraph:BATCH-X"] * 3
    bases = env.git("log", "--merges", "--format=%(trailers:key=Land-Base,valueonly)", f"{state['base_head']}..{state['head']}",
                    cwd=env.repo)
    assert bases.split() == [state["base_head"]] * 3  # the tested base is part of the tested commit


def test_tested_ff_lands_a_batch_whose_key_is_in_git_and_the_receipt_keeps_the_citations(env):
    approval = pushed(env, plan="plangraph:BATCH-X")
    mode(env, "tested-ff")
    head = head_of(env)
    assert land(env, approval).returncode == 0 and main_tip(env) == head
    (receipt,) = (env.repo / ".git" / "merge-batch" / "receipts").glob("l1-*.json")
    data = json.loads(receipt.read_text())
    assert data["plan_item"] == "plangraph:BATCH-X" and data["review_runs"]["1"] == {"plangraph:ITEM-A": "run-part-1-0"}


def test_tested_ff_refuses_a_batch_whose_plan_key_is_not_in_git(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    state = json.loads(env.state_file("l1").read_text())
    state["plan"] = "plangraph:BATCH-X"  # built before the key was written into the merge commits
    env.state_file("l1").write_text(json.dumps(state))
    res = land(env, approval)
    assert res.returncode == 1 and "no Plan-Item trailer of a batch commit" in res.stderr, res.stderr
    assert main_tip(env) != state["head"] and rows(env) == []


def test_a_local_keeper_json_that_says_false_does_not_stand_in_for_the_acknowledgement(env):
    approval = pushed(env, plan="plangraph:BATCH-X")
    mode(env, "tested-ff")
    del env.env["PG_MERGE_CLOSE_REQUIRE_SEATS"]
    config = env.home / ".config" / "plangraph" / "keeper.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "require_seats": False}))
    config.chmod(0o600)
    res = land(env, approval)
    assert res.returncode == 1 and "PG_MERGE_CLOSE_REQUIRE_SEATS" in res.stderr and rows(env) == []



def test_tested_ff_refuses_a_head_whose_land_base_trailer_is_not_the_recorded_base(env):
    approval = pushed(env)
    mode(env, "tested-ff")
    state = json.loads(env.state_file("l1").read_text())
    state["base_head"] = state["head"]  # a state that names another base than the head's own trailer
    env.state_file("l1").write_text(json.dumps(state))
    res = land(env, approval)
    assert res.returncode == 1 and "Land-Base" in res.stderr, res.stderr
    assert rows(env) == [] and main_tip(env) != state["head"]


@pytest.mark.parametrize("value,refused", [("true", True), ("on", True), ("1", True), ("maybe", True), ("", True),
                                           ("false", False), ("off", False), ("0", False)])
def test_tested_ff_needs_the_explicit_seat_acknowledgement(env, value, refused):
    """The daemon's require_seats cannot be read from here, so tested-ff lands only when the operator says
    PG_MERGE_CLOSE_REQUIRE_SEATS=off|false|0, the value the lander's own merge-close then sends."""
    approval = pushed(env, plan="plangraph:BATCH-X")
    mode(env, "tested-ff")
    if value:
        env.env["PG_MERGE_CLOSE_REQUIRE_SEATS"] = value
    else:
        del env.env["PG_MERGE_CLOSE_REQUIRE_SEATS"]
    res = land(env, approval)
    assert (res.returncode == 1 and "PG_MERGE_CLOSE_REQUIRE_SEATS" in res.stderr) == refused, res.stderr
    assert (rows(env) == []) == refused


@pytest.mark.parametrize("message,refused", [
    ("train\n\nLand-Base: {base}", False),
    ("train\n\nLand-Base: {base}\nLand-Base: {head}", True),     # conflicting trailers
    ("train\n\nLand-Base: {head}\n\nsigned off\n\nLand-Base: {base}", False),  # an earlier body line is no trailer
    ("train\nLand-Base: {base}\n\nno trailer block here", True),  # a body line only, no trailer paragraph
    ("train\n\nLand-Base: {base}\nLand-Base:", True),            # a valid trailer and an empty one
])
def test_tested_ff_reads_exactly_one_land_base_trailer(env, tools_importable, monkeypatch, message, refused):
    pushed(env)
    state = json.loads(env.state_file("l1").read_text())
    env.git("checkout", "-q", "-B", "trailers", state["head"], cwd=env.repo)
    env.git("commit", "-q", "--allow-empty", "-m", message.format(base=state["base_head"], head=state["head"]),
            cwd=env.repo)
    state["head"] = env.git("rev-parse", "HEAD", cwd=env.repo)
    env.git("checkout", "-q", "main", cwd=env.repo)
    import merge_batch_land
    monkeypatch.setenv("PG_MERGE_CLOSE_REQUIRE_SEATS", "off")
    monkeypatch.setenv("HOME", str(env.home))
    state["plan"] = None
    if refused:
        with pytest.raises(merge_batch_land.MergeBatchError, match="Land-Base"):
            merge_batch_land.require_tested_ff_evidence(state)
    else:
        merge_batch_land.require_tested_ff_evidence(state)
