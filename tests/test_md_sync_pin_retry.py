"""Pin-mismatch retry for md_sync --apply (synthetic fixtures, no live keeper)."""
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

import apply_batch  # noqa: E402
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


def status_of(path):
    return json.loads(path.read_text(encoding="utf-8"))


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


RETRY_LINE = "pin mismatch: source changed between read and apply; retrying once after 5s"


def test_rewritten_answers_retry_once_then_applies(world, srv, token, site,
                                                   tmp_path, monkeypatch):
    """ANSWERS.md changes mid-run: one wait, fresh re-read, single apply."""
    synthetic(site)
    status = tmp_path / "md-sync.status.json"
    sleeps = []
    monkeypatch.setattr(md_sync, "SLEEP", lambda s: sleeps.append(s))
    real_hash = apply_batch.sha256_of
    calls = []

    def racing(path):
        calls.append(1)
        if len(calls) == 1:
            with open(site / "ANSWERS.md", "a", encoding="utf-8") as f:
                f.write("\n")
        return real_hash(path)

    monkeypatch.setattr(apply_batch, "sha256_of", racing)
    before = rows(world, "SELECT COUNT(*) FROM events")[0][0]
    rc, out, err = sync(site, srv, token, "--apply",
                        "--status-file", str(status))
    assert RETRY_LINE in out
    assert sleeps == [5]
    assert rc == 0, out + err
    assert out.count("sha256 mismatch") == 1
    assert out.count("applied 4 of 4") == 1
    assert state(world, "routing:Q-SHAPY") == "open"
    after = rows(world, "SELECT COUNT(*) FROM events")[0][0]
    assert after > before
    doc = status_of(status)
    assert (doc["applied"], doc["error"]) == (True, None)


def test_second_mismatch_gives_up_with_last_error(world, srv, token, site,
                                                  tmp_path, monkeypatch):
    """Two pins disagree in a row: exactly one wait, no keeper writes."""
    synthetic(site)
    status = tmp_path / "md-sync.status.json"
    sleeps = []
    monkeypatch.setattr(md_sync, "SLEEP", lambda s: sleeps.append(s))
    real_hash = apply_batch.sha256_of

    def always_race(path):
        with open(site / "ANSWERS.md", "a", encoding="utf-8") as f:
            f.write("\n")
        return real_hash(path)

    monkeypatch.setattr(apply_batch, "sha256_of", always_race)
    before = (rows(world, "SELECT COUNT(*) FROM items")[0][0],
              rows(world, "SELECT COUNT(*) FROM events")[0][0])
    rc, out, err = sync(site, srv, token, "--apply",
                        "--status-file", str(status))
    assert sleeps == [5]
    assert rc == 1
    assert out.count(RETRY_LINE) == 1
    doc = status_of(status)
    assert doc["applied"] is False
    assert doc["error"].strip().startswith("source ")
    assert "sha256 mismatch" in doc["error"]
    assert doc["error"].endswith("(refusing before any call)")
    assert "\n" not in doc["error"]
    assert (rows(world, "SELECT COUNT(*) FROM items")[0][0],
            rows(world, "SELECT COUNT(*) FROM events")[0][0]) == before


def test_keeper_refusal_does_not_sleep(world, srv, token, site, tmp_path,
                                       monkeypatch):
    """A real keeper refusal is not a pin mismatch: no wait, same rc."""
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
    sleeps = []
    monkeypatch.setattr(md_sync, "SLEEP", lambda s: sleeps.append(s))
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--apply",
                        "--status-file", str(status))
    assert rc == 1
    assert "nothing applied" in out
    assert "sha256 mismatch" not in out
    assert RETRY_LINE not in out
    assert sleeps == []


def test_clean_apply_never_sleeps(world, srv, token, site, tmp_path,
                                  monkeypatch):
    """No mismatch anywhere: the wait is never touched."""
    synthetic(site)
    sleeps = []
    monkeypatch.setattr(md_sync, "SLEEP", lambda s: sleeps.append(s))
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--apply",
                        "--status-file", str(status))
    assert rc == 0, out + err
    assert RETRY_LINE not in out
    assert sleeps == []
    doc = status_of(status)
    assert (doc["applied"], doc["error"]) == (True, None)


def test_retry_is_bounded_to_one_wait_of_five_seconds(world, srv, token, site,
                                                      tmp_path, monkeypatch):
    """Mutation guard: unlimited retries or a skipped wait fail here."""
    synthetic(site)
    sleeps = []
    monkeypatch.setattr(md_sync, "SLEEP", lambda s: sleeps.append(s))
    real_hash = apply_batch.sha256_of

    def always_race(path):
        with open(site / "ANSWERS.md", "a", encoding="utf-8") as f:
            f.write("\n")
        return real_hash(path)

    monkeypatch.setattr(apply_batch, "sha256_of", always_race)
    applies = []
    real_apply = md_sync._apply

    def counting(path, opts, out, err):
        applies.append(1)
        return real_apply(path, opts, out, err)

    monkeypatch.setattr(md_sync, "_apply", counting)
    status = tmp_path / "md-sync.status.json"
    rc, out, err = sync(site, srv, token, "--apply",
                        "--status-file", str(status))
    assert rc == 1
    assert applies == [1, 1]
    assert sleeps == [5]
