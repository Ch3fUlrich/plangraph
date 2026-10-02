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
    assert out.rstrip().endswith("close-skipped 1")

    doc = status_of(status)
    assert list(doc) == ["ts", "mode", "ops", "applied", "skipped", "refused",
                         "gone", "redactions", "close_skipped", "error"]
    assert doc["mode"] == "dry" and doc["applied"] is False and doc["error"] is None
    assert doc["close_skipped"] == [{"id": "Q-ALPHA", "decision": "D-10",
                                     "key": "routing:Q-ALPHA"}]
    counts = {k: n for k, n in tally(out).items() if k != "close_skipped"}
    assert all(doc[name] == n for name, n in counts.items())
    assert len(doc["close_skipped"]) == tally(out)["close_skipped"]
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
    counts = {k: n for k, n in tally(out).items() if k != "close_skipped"}
    assert all(doc[name] == n for name, n in counts.items())


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
