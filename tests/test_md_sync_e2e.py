"""md_sync v1 against a real keeper (in-process HTTP server), synthetic fixtures."""
import hashlib
import io
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import md_sync  # noqa: E402

from keeper_util import World, defn  # noqa: E402
from plangraph.keeper import api, config  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402
from plangraph.keeper.evidence_git import GitAdapter, LegacyCommitVerifier  # noqa: E402
from test_keeper_merge_close_auto import Repo, env, git  # noqa: E402,F401 - env fixture

FIX = ROOT / "tests" / "fixtures" / "md_sync"


@pytest.fixture
def site(tmp_path):
    """A stand-in for the router checkout: the batch root the keeper trusts."""
    site = tmp_path / "site"
    site.mkdir()
    # The fixture files carry deny-safe names; they land under the site's own
    # QUESTIONS.md / ANSWERS.md names (temp paths, never scanned).
    renamed = {"QUESTIONS.md": "sample_questions.md",
               "ANSWERS.md": "sample_answers.md"}
    for name in ("QUESTIONS.md", "ANSWERS.md", "decisions-log.md"):
        shutil.copy(FIX / renamed.get(name, name), site / name)
    return site


@pytest.fixture
def world(tmp_path, site):
    w = World(tmp_path)
    r = w.p("router")
    w.store.create_items(r, [
        defn("R-1", project="routing"),
        defn("Q-SHARED", project="server"),
        defn("GOAL", project="routing", kind="goal",
             deps=["routing:R-1", "server:Q-SHARED"])])
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(site / "decisions-log.md"))
    w.store.batch_roots = [str(site)]
    yield w
    w.close()


@pytest.fixture
def srv(world, tmp_path):
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(world.path), "bind": "127.0.0.1:0",
                               "snapshot": str(tmp_path / "plan.snapshot.json")}))
    os.chmod(cfg, 0o600)
    server = api.make_server(world.store, config.load_config(cfg), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def token(world, tmp_path):
    path = tmp_path / "router.token"
    path.write_text(world.tokens["router"], encoding="utf-8")
    return str(path)


def sync(site, srv, token, *extra, answers="ANSWERS.md"):
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--questions", str(site / "QUESTIONS.md"),
                       "--answers", str(site / answers),
                       "--decisions", str(site / "decisions-log.md"),
                       "--url", srv.base_url, "--token-file", token, *extra],
                      out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def rows(world, sql, *args):
    return world.conn.execute(sql, args).fetchall()


def state(world, key):
    return rows(world, "SELECT state FROM items WHERE key=?", key)[0][0]


def test_dry_run_prints_plan_and_changes_nothing(world, srv, token, site):
    before = rows(world, "SELECT COUNT(*) FROM events")[0][0]
    rc, out, err = sync(site, srv, token, "--dry-run")
    assert rc == 1  # Q-GHOST cites D-999, which the decisions log lacks
    assert "plan 1 create routing:Q-ALPHA" in out
    assert "refused Q-GHOST: unknown decision D-999" in out
    assert "no D-id" in out and "Q-BETA-2" in out
    assert "redactions 1" in out and "(no calls made)" in out
    assert rows(world, "SELECT COUNT(*) FROM events")[0][0] == before


def test_apply_creates_closes_links_and_second_run_is_empty(world, srv, token, site):
    rc, out, err = sync(site, srv, token, "--apply")
    assert rc == 1, out + err  # applied, but Q-GHOST was refused and reported
    assert "refused Q-GHOST" in out and "summary:" in out
    assert state(world, "routing:Q-ALPHA") == "done"
    assert state(world, "routing:OS-1") == "done"
    assert state(world, "routing:OS-2") == "done"
    assert state(world, "server:Q-SHARED") == "done"   # reused across projects
    assert state(world, "routing:Q-GHOST") == "open"
    assert rows(world, "SELECT COUNT(*) FROM items WHERE key='routing:Q-SHARED'")[0][0] == 0
    gate = rows(world, "SELECT key FROM items WHERE key='routing:PH-OPERATOR'")
    assert len(gate) == 1
    desc = rows(world, "SELECT description FROM items WHERE key='routing:OS-1'")[0][0]
    assert "Zx81kQ92LmPq77Vb" not in desc and "[redacted]" in desc
    note = rows(world, "SELECT note FROM items WHERE key='routing:OS-1'")[0][0]
    assert note is None or "Zx81kQ92LmPq77Vb" not in note
    rc2, out2, _ = sync(site, srv, token, "--dry-run")
    assert "dry run: no ops" in out2


def test_close_uses_answers_path_and_sha_as_batch_ref(world, srv, token, site):
    sync(site, srv, token, "--apply")
    sha = hashlib.sha256((site / "ANSWERS.md").read_bytes()).hexdigest()
    payloads = [json.loads(r[0]) for r in rows(
        world, "SELECT payload FROM events WHERE type='closed_on_decision'")]
    assert payloads and all(
        p["batch_ref"] == f"{site / 'ANSWERS.md'}@{sha}" and p["sha256"] == sha
        for p in payloads)


def test_gate_is_created_once_across_runs(world, srv, token, site):
    sync(site, srv, token, "--apply")
    with open(site / "QUESTIONS.md", "a", encoding="utf-8") as f:
        f.write("- Q-LATE **Added later**\n")
    rc, out, _ = sync(site, srv, token, "--dry-run")
    assert "create routing:Q-LATE" in out and "create routing:PH-OPERATOR" not in out
    assert "edit routing:PH-OPERATOR" in out and "edit routing:GOAL" not in out


def tally(out):
    """The summary line as numbers, keyed ``close_skipped`` (status spelling)."""
    text = [ln for ln in out.splitlines() if ln.startswith("md-sync: ")][-1]
    pairs = (part.strip().split(" ", 1)
             for part in text[len("md-sync: "):].rstrip().split(", "))
    return {name.replace("-", "_"): int(n) for name, n in pairs}


def status_of(path):
    return json.loads(path.read_text(encoding="utf-8"))


def closes_elsewhere(site, elsewhere="routing:Q-OTHER"):
    """D-10's DECISION cell closes somebody else: Q-ALPHA's close would be refused."""
    log = site / "decisions-log.md"
    log.write_text(log.read_text(encoding="utf-8").replace(
        "| use sqlite for the widget cache |", f"| Closes: {elsewhere} |"),
        encoding="utf-8")


def test_close_skipped_is_planned_away_and_recorded(world, srv, token, site, tmp_path):
    """A close the keeper would refuse: no ops, one report line, id in the status."""
    closes_elsewhere(site)
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--dry-run", "--status-file", str(status))
    assert rc == 1, out + err  # Q-GHOST still cites D-999
    assert "close-skipped Q-ALPHA: D-10 Closes does not list routing:Q-ALPHA" in out
    for verb in ("legacy_state", "add_evidence", "close_on_decision"):
        assert f"{verb} routing:Q-ALPHA" not in out
    assert "create routing:Q-ALPHA" in out   # the lane itself still comes from QUESTIONS
    assert out.rstrip().endswith("close-skipped 1, d-closes 0, in-review 0, warnings 0")

    doc = status_of(status)
    assert list(doc) == ["ts", "mode", "ops", "applied", "skipped", "refused",
                         "gone", "redactions", "close_skipped", "choices_skipped", "d_closes",
                         "d_in_review", "warnings", "models", "unmatched", "error"]
    assert doc["mode"] == "dry" and doc["applied"] is False and doc["error"] is None
    assert doc["close_skipped"] == [{"id": "Q-ALPHA", "decision": "D-10",
                                     "key": "routing:Q-ALPHA",
                                     "reason": "Closes does not list the key"}]
    counts = {k: n for k, n in tally(out).items()
              if k not in ("close_skipped", "d_closes", "in_review", "warnings")}
    assert all(doc[name] == n for name, n in counts.items())
    assert len(doc["close_skipped"]) == tally(out)["close_skipped"]
    assert len(doc["d_closes"]) == tally(out)["d_closes"]
    assert len(doc["d_in_review"]) == tally(out)["in_review"]
    assert len(doc["warnings"]) == tally(out)["warnings"]
    assert doc["models"] == []
    # ids and counts only: no title, decision text or description may leak
    raw = status.read_text(encoding="utf-8")
    for text in ("Pick a storage backend", "widget cache", "redacted"):
        assert text not in raw


def test_apply_writes_status_with_applied_and_no_error(world, srv, token, site, tmp_path):
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--apply", "--status-file", str(status))
    assert rc == 1, out + err  # applied; Q-GHOST was refused and reported
    doc = status_of(status)
    assert (doc["mode"], doc["applied"], doc["error"]) == ("apply", True, None)
    assert doc["close_skipped"] == []
    assert doc["d_closes"] == []
    assert doc["d_in_review"] == []
    assert doc["warnings"] == []
    assert doc["models"] == []
    counts = {k: n for k, n in tally(out).items()
              if k not in ("close_skipped", "d_closes", "in_review", "warnings")}
    assert all(doc[name] == n for name, n in counts.items())
    assert len(doc["close_skipped"]) == tally(out)["close_skipped"]
    assert len(doc["d_closes"]) == tally(out)["d_closes"]
    assert len(doc["d_in_review"]) == tally(out)["in_review"]
    assert len(doc["warnings"]) == tally(out)["warnings"]


def test_status_file_is_written_when_the_run_fails_early(world, srv, token, site,
                                                         tmp_path):
    """Exit 2 still leaves a record: the router must not have to guess."""
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--dry-run", "--status-file", str(status),
                        answers="NOPE.md")
    assert rc == 2 and "NOPE.md" in err
    doc = status_of(status)
    assert doc["mode"] == "dry" and doc["applied"] is False
    assert doc["ops"] == 0 and doc["close_skipped"] == []
    assert doc["error"].startswith("error:") and "NOPE.md" in doc["error"]
    assert status.read_text(encoding="utf-8").count("\n") == 1  # one JSON object


def test_status_file_is_written_on_bad_arguments(world, srv, token, site, tmp_path):
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--apply", "--dry-run",
                        "--status-file", str(status))
    assert rc == 2
    doc = status_of(status)
    assert (doc["mode"], doc["error"]) == ("apply", "error: bad arguments (see usage)")


def test_half_failing_apply_leaves_db_unchanged(world, srv, token, site, tmp_path,
                                                monkeypatch):
    """The refusal must come from the keeper, i.e. after plan time.

    md_sync pre-checks ``Closes:`` and would drop Q-ALPHA's whole item before
    the keeper ever saw it, so D-10 is tampered inside ``write_batch`` -- the
    plan is already built -- and the keeper alone refuses.  The batch is one
    atomic ``apply_ops``: everything rolls back, and the status still records
    the failed run.
    """
    log = site / "decisions-log.md"
    original = log.read_text(encoding="utf-8")
    real = md_sync.write_batch

    def tamper(*a, **k):
        path = real(*a, **k)
        log.write_text(original.replace(
            "| use sqlite for the widget cache |", "| Closes: routing:Q-OTHER |"),
            encoding="utf-8")
        return path

    monkeypatch.setattr(md_sync, "write_batch", tamper)
    before = (rows(world, "SELECT COUNT(*) FROM items")[0][0],
              rows(world, "SELECT COUNT(*) FROM events")[0][0])
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--apply", "--status-file", str(status))
    assert rc == 1 and "nothing applied" in out
    assert (rows(world, "SELECT COUNT(*) FROM items")[0][0],
            rows(world, "SELECT COUNT(*) FROM events")[0][0]) == before
    doc = status_of(status)
    assert (doc["mode"], doc["applied"]) == ("apply", False)
    assert isinstance(doc["error"], str) and doc["error"].strip() == doc["error"]
    assert doc["error"] and "\n" not in doc["error"]
    assert not doc["error"].startswith("acting as ")  # the refusal line, not the login


def test_answers_changed_between_read_and_apply_is_refused(world, srv, token, site, monkeypatch):
    monkeypatch.setattr(md_sync, "SLEEP", lambda _s: None)
    real = md_sync.write_batch

    def tamper(*a, **k):
        path = real(*a, **k)
        with open(site / "ANSWERS.md", "a", encoding="utf-8") as f:
            f.write("\n")
        return path

    monkeypatch.setattr(md_sync, "write_batch", tamper)
    rc, out, _ = sync(site, srv, token, "--apply")
    assert rc == 1 and "sha256 mismatch" in out
    assert state(world, "routing:R-1") == "open"
    assert rows(world, "SELECT COUNT(*) FROM items WHERE key='routing:Q-ALPHA'")[0][0] == 0


def test_bad_token_file_exits_2(world, srv, site, tmp_path):
    rc, out, err = sync(site, srv, str(tmp_path / "missing"), "--dry-run")
    assert rc == 2 and "token" in err


def test_missing_input_file_exits_2(world, srv, token, site):
    rc, _, err = sync(site, srv, token, "--dry-run", answers="NOPE.md")
    assert rc == 2 and "NOPE.md" in err


def test_apply_and_dry_run_are_exclusive(world, srv, token, site):
    rc, _, _ = sync(site, srv, token, "--apply", "--dry-run")
    assert rc == 2


# synthetic, not a credential
SHAPED_ROW = "- Q-SHAPY **Rotate the key** " + "login see add/" + "commitx\n"

SHAPED_QUESTIONS = (
    "# Open questions (SYNTHETIC fixture, not site data)\n\n"
    "## Questions for the router\n"
    + SHAPED_ROW
    + "- Q-PLAIN **Rotate the routine key** plain words only\n")

QUIET_ANSWERS = "# Answers (SYNTHETIC fixture, not site data)\n"


def synthetic(site, questions=SHAPED_QUESTIONS, answers=QUIET_ANSWERS):
    (site / "QUESTIONS.md").write_text(questions, encoding="utf-8")
    (site / "ANSWERS.md").write_text(answers, encoding="utf-8")


def test_shape_row_is_masked_and_the_apply_still_succeeds(world, srv, token, site):
    """D-405: masking happens before the send, so the keeper never refuses."""
    synthetic(site)
    rc, out, err = sync(site, srv, token, "--dry-run")
    assert rc == 0, out + err
    assert "create routing:Q-SHAPY" in out and "redactions 1" in out
    rc, out, err = sync(site, srv, token, "--apply")
    assert rc == 0, out + err
    assert "refused 0," in out and "applied 4 of 4" in out
    assert "error:" not in out and "nothing applied" not in out
    title, description = rows(world,
                              "SELECT title, description FROM items WHERE key=?",
                              "routing:Q-SHAPY")[0]
    assert title == "Q: Rotate the key"
    assert description == "login see [redacted]"
    assert "commitx" not in description
    plain = rows(world, "SELECT description FROM items WHERE key=?",
                 "routing:Q-PLAIN")[0][0]
    assert plain == "plain words only"  # no shape: untouched
    assert state(world, "routing:Q-SHAPY") == "open"
    rc, out, err = sync(site, srv, token, "--dry-run")
    assert rc == 0 and "dry run: no ops" in out  # masked text matches the keeper


def test_original_value_never_reaches_stdout_or_stderr(world, srv, token, site,
                                                       capsys):
    synthetic(site)
    rc = md_sync.main(["--questions", str(site / "QUESTIONS.md"),
                       "--answers", str(site / "ANSWERS.md"),
                       "--decisions", str(site / "decisions-log.md"),
                       "--url", srv.base_url, "--token-file", token, "--apply"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "redactions 1" in captured.out
    # synthetic, not a credential
    for original in ("add/" + "commitx", "login see " + "add/" + "commitx"):
        assert original not in captured.out
        assert original not in captured.err


def test_sync_md_import_questions_are_question_kind_and_land_in_answers(world, srv, token, site):
    """SYNC-MD-IMPORT: a new QUESTIONS row is a MODEL-KINDS question (or operator
    step), and an answered row lands in Answers (keeper read + snapshot) with its
    D-id, even though md-sync closes it by decision (no answer text in the keeper)."""
    sync(site, srv, token, "--apply")
    kinds = dict(rows(world, "SELECT key, kind FROM items WHERE track='QUESTIONS' AND kind!='gate'"))
    assert kinds["routing:Q-ALPHA"] == "question"
    assert set(kinds.values()) <= {"question", "operator_task"}
    reader = world.p("reader")
    got = {r["key"]: r for r in world.store.answers(reader)}
    alpha = got["routing:Q-ALPHA"]
    assert alpha["decision_id"].startswith("D-")
    assert alpha["answer"].startswith("answered by D-")
    assert alpha["done_at"]
    assert "routing:Q-GHOST" not in got          # still open
    snap = {i["key"]: i for i in world.store.snapshot(reader)["items"]}
    assert snap["routing:Q-ALPHA"]["answer"]["decision_id"] == alpha["decision_id"]
    assert snap["routing:Q-GHOST"]["answer"] is None


def test_a_close_the_keeper_gate_would_refuse_is_held_and_the_rest_of_the_import_goes_on():
    """The keeper refuses the close of an item with neither a deploy row nor a no-deploy reason, which would
    roll the whole batch back: the importer reports the close as held and plans no op that needs it."""
    import md_sync_plan

    def planned(item):
        plan = SimpleNamespace(skipped=[], ops=[])
        plan.add = plan.ops.append
        md_sync_plan._close(plan, dict(item, key="demo:Q", state="in_review", review_tier="none",
                                       evidence_kinds=["decision"], evidence=[]), ["D-1"])
        return plan

    held = planned({"note": "plain note"})
    assert held.ops == [] and "held: live_evidence_missing" in held.skipped[0][1]
    live = planned({"note": "no deploy needed: decision only", "done_criteria": "Live: it runs."})
    assert live.ops == [] and "held" in live.skipped[0][1], "a no-deploy line never excuses a Live clause"
    ok = planned({"note": "no deploy needed: decision only"})
    assert [op["op"] for op in ok.ops] == ["add_evidence", "close_on_decision"] and ok.skipped == []


def test_snapshot_deploy_proof_closes_answers_and_both_d_row_paths(world, srv, token, site, tmp_path):
    from keeper_util import FakeVerifier

    router = world.p("router")
    world.store.verifiers["deploy"] = FakeVerifier()
    world.store.verifiers["commit"] = LegacyCommitVerifier()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = Repo(repo_path)
    head = repo.land("merged", "fixture", 42)
    repos = {"routing": {"path": str(repo_path), "ref": "main"}}
    world.store.ctx.git = GitAdapter(repos)
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps(dict(json.loads(cfg.read_text()), repos=repos)), encoding="utf-8")
    world.clock.t = int(git(repo_path, "show", "-s", "--format=%ct", "main")) + 60

    ids = ("Q-ANS", "DEC", "CODE", "Q-EPOCH", "Q-UNVERIFIED", "STALE")
    world.store.create_items(router, [
        defn(ident, project="routing", evidence_kinds=("decision", "commit") if ident in ("CODE", "STALE")
             else ("decision",), review_tier="none", done_criteria="Live: observe it running.")
        for ident in ids
    ] + [defn("LIVE-GOAL", ids, project="routing", kind="goal")])
    for ident in ("Q-ANS", "DEC", "CODE", "Q-UNVERIFIED", "STALE"):
        world.store.set_legacy_state(router, f"routing:{ident}", "in_review")

    proof = {"host": "fixture", "unit": "test.service", "is_active": "active"}
    for ident in ("Q-ANS", "DEC", "CODE", "Q-EPOCH"):
        assert world.store.add_evidence_router(router, f"routing:{ident}", "deploy", proof)["verified"]
    world.store.verifiers["deploy"].ok = False
    assert not world.store.add_evidence_router(router, "routing:Q-UNVERIFIED", "deploy", proof)["verified"]
    world.store.verifiers["deploy"].ok = True
    world.clock.t -= 120
    assert world.store.add_evidence_router(router, "routing:STALE", "deploy", proof)["verified"]
    world.clock.advance(121)

    (site / "QUESTIONS.md").write_text("", encoding="utf-8")
    (site / "ANSWERS.md").write_text("## 2026-10-01\n"
                                     "- Q-ANS answer: done (D-10)\n"
                                     "- Q-EPOCH answer: done (D-10)\n"
                                     "- Q-UNVERIFIED answer: done (D-10)\n", encoding="utf-8")
    with (site / "decisions-log.md").open("a", encoding="utf-8") as log:
        log.write("| D-20 | routing | Closes: routing:DEC | router | fixture |\n"
                  f"| D-21 | routing | Closes: routing:CODE@{head} | router | fixture |\n"
                  f"| D-22 | routing | Closes: routing:STALE@{head} | router | fixture |\n")

    rc, out, err = sync(site, srv, token, "--apply", "--keeper-config", str(cfg))
    assert rc == 0, out + err
    assert {ident for ident in ids if state(world, f"routing:{ident}") == "done"} == {"Q-ANS", "DEC", "CODE"}, out
    assert "held: live_evidence_missing for routing:Q-EPOCH" in out
    assert "held: live_evidence_missing for routing:Q-UNVERIFIED" in out
    assert "refused routing:STALE: skipped, re-applying the rest" in out
    assert state(world, "routing:Q-EPOCH") == "in_review"
    assert world.store._item("routing:Q-EPOCH")["epoch"] != world.store._evidence("routing:Q-EPOCH")[0]["epoch"]
    rc, out, err = sync(site, srv, token, "--dry-run", "--keeper-config", str(cfg))
    assert rc == 0 and "held: live_evidence_missing for routing:Q-EPOCH" in out, out + err


def test_track_commit_evidence_uses_its_configured_landing_ref(env, monkeypatch, tmp_path):
    from test_keeper_deploy_gate_paths import _deploy_row, _live_item, _refused_verdict

    key = "demo:B"
    _live_item(env, key)
    track = tmp_path / "track"
    track.mkdir()
    for variable in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(variable, "@1 +0000")
    Repo(track)
    git(track, "checkout", "-q", "-b", "topic")
    for variable in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(variable, "@100 +0000")
    git(track, "commit", "-q", "--allow-empty", "-m", "work on topic")
    topic = git(track, "rev-parse", "HEAD")
    git(track, "checkout", "-q", "-b", "release", "main")
    for variable in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(variable, "@300 +0000")
    git(track, "merge", "-q", "--no-ff", "-m", "Merge pull request #99 from fixture/topic", "topic")

    adapter = GitAdapter({"demo": str(env.repo.root),
                          "demo/T0": {"path": str(track), "ref": "release"}},
                         merge_scan=["demo"])
    env.store.ctx["git"] = adapter
    env.store.verifiers["commit"] = LegacyCommitVerifier()
    assert env.store.add_evidence_router(env.p("router"), key, "commit",
                                         {"sha": topic, "repo": "demo/T0"})["verified"]
    item = env.store._item(key)
    assert env.store._sha_landed_at("demo/T0", topic, "main") == 100
    _deploy_row(env, key, "test.service", 250, is_active="active")
    _refused_verdict(env, key)
    assert env.store._landing_floor(env.store._pin_close(item)) == 300
    _deploy_row(env, key, "test.service", 300, is_active="active")
    assert env.store.verdict(env.p("reviewer"), key, True, "passed")["state"] == "done"
