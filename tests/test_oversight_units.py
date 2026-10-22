"""RT-OVERSIGHT-CHECKS unit tests: digest, alarms, source guard, units, CLI.

Hermetic TDD for the oversight lane: no network, no LLM, an injected clock and a
``keeper_util.World`` sqlite snapshot.  ``plangraph.keeper.oversight`` and
``plangraph.keeper.cli_oversight`` are imported *inside* the tests, so this file
is RED with ``ModuleNotFoundError`` until the lane lands, and RED with a
missing-file assertion until ``deploy/systemd/plangraph-oversight-check.*``
exists.

Spec (flow-oversight-spec.md): findings sorted by (rule, key); ``digest``
byte-identical over two runs of one snapshot with the sha256 recorded in
``digest.sha256``; ``raise_alarms`` dedupes by reason and clears vanished rows;
``run_checks`` lists the idle claimed P1 lanes; oversight.py imports no
LLM/network stack; the timer fires every 30 minutes; ``oversight-check
--out-dir DIR`` on a tmp db writes both digest files and exits 0.

The oversight imports inside the tests read ``import plangraph.keeper.oversight
as oversight`` (and ``... cli_oversight ...``): the keeper package's lazy
``__getattr__`` would turn ``from plangraph.keeper import oversight`` into a
plain ``ImportError``, while the lane must stay RED with ``ModuleNotFoundError``
until the modules exist.
"""
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper.events import append_event

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
OVERSIGHT_SRC = ROOT / "src" / "plangraph" / "keeper" / "oversight.py"
BANNED = re.compile(r"anthropic|openai|requests|urllib|http\.client|socket")
FINDING_KEYS = {"rule", "kind", "key", "reason", "detail", "ts"}
HOUR = 3600.0


@pytest.fixture(autouse=True)
def _fresh_stall_cache(monkeypatch):
    """No stall cache carried between tests (same trick as ``test_stall_alarms``)."""
    from plangraph.keeper import stall
    monkeypatch.setattr(stall, "_CACHE_TTL_S", 0.0)
    stall._cache.clear()


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def heartbeat(w, key, ts=None):
    """A heartbeat for *key* stamped *ts* (default: the injected clock)."""
    append_event(w.conn, w.store._iso(w.clock() if ts is None else ts),
                 "router", "router", None, key, "heartbeat", {})
    w.conn.commit()


def seed_claimed(w, *lanes, priority=0):
    """Create *lanes* plus their goal, set the priority, claim and heartbeat."""
    keys = seed(w, list(lanes))
    for key in keys:
        if priority:
            w.store.set_priority(w.p("router"), key, priority)
        to_claimed(w, key, who="l1main", lease_s=48 * HOUR)
        heartbeat(w, key)
    return keys


# ------------------------------------------------------------------- digest
def test_digest_is_byte_identical_across_two_runs(w):
    import plangraph.keeper.oversight as oversight     # red: ModuleNotFoundError
    key = seed_claimed(w, ("A", ()), priority=1)[0]
    w.clock.advance(25 * HOUR)                       # idle: past LANE_IDLE_S
    text1, sha1 = oversight.digest(oversight.run_checks(w.conn, w.clock()))
    text2, sha2 = oversight.digest(oversight.run_checks(w.conn, w.clock()))
    assert text1.encode("ascii") == text2.encode("ascii")     # byte-identical
    assert sha1 == sha2
    assert sha1 == hashlib.sha256(text1.encode("ascii")).hexdigest()
    findings = json.loads(text1)                     # parseable, stable payload
    assert findings and all(FINDING_KEYS <= set(f) for f in findings)
    assert any(f["rule"] == "R1" and f["key"] == key for f in findings)


# -------------------------------------------------------------- raise_alarms
def test_raise_alarms_dedupes_and_clears_vanished_rows(w):
    import plangraph.keeper.oversight as oversight     # red: ModuleNotFoundError
    now = w.clock()
    stall = {"rule": "R1", "kind": "stall_no_event", "key": "demo:A",
             "reason": "R1:stall_no_event:demo:A",
             "detail": "claimed, no event for 90000s (last 25h)",
             "ts": w.store._iso(now)}
    overdue = {"rule": "R5", "kind": "question_overdue", "key": "demo:Q1",
               "reason": "R5:question_overdue:demo:Q1",
               "detail": "deadline passed", "ts": w.store._iso(now)}
    findings = [stall, overdue]
    assert oversight.raise_alarms(w.conn, findings, now) == 2
    assert oversight.raise_alarms(w.conn, findings, now) == 0   # deduped by reason
    assert oversight.raise_alarms(w.conn, findings[:1], now) == 0   # vanished row
    rows = w.conn.execute(
        "SELECT kind, key, detail, cleared_at FROM projector_alarms"
        " WHERE workflow_id='oversight' ORDER BY id").fetchall()
    assert [(r["kind"], r["key"]) for r in rows] == [
        ("stall_no_event", "demo:A"), ("question_overdue", "demo:Q1")]
    assert rows[0]["cleared_at"] is None              # unchanged finding stays open
    assert rows[1]["cleared_at"]                      # vanished finding is cleared
    assert stall["reason"] in rows[0]["detail"]
    assert overdue["reason"] in rows[1]["detail"]


# --------------------------------------------------- run_checks / P1 lanes
def test_run_checks_lists_the_idle_claimed_p1_lanes(w):
    import plangraph.keeper.oversight as oversight     # red: ModuleNotFoundError
    keys = seed_claimed(w, ("A", ()), ("B", ()), ("C", ()), priority=1)
    assert all(w.conn.execute("SELECT priority FROM items WHERE key=?",
                              (key,)).fetchone()[0] == 1 for key in keys)
    w.clock.advance(25 * HOUR)          # A and B idle for longer than LANE_IDLE_S
    heartbeat(w, keys[2])               # ...but C kept working, so C is not idle
    findings = oversight.run_checks(w.conn, w.clock())
    assert findings == sorted(findings, key=lambda f: (f["rule"], f["key"]))
    assert [(f["rule"], f["kind"], f["key"])
            for f in findings if f["rule"] == "R1"] == [
        ("R1", "stall_no_event", keys[0]), ("R1", "stall_no_event", keys[1])]
    assert all(f["key"] != keys[2] for f in findings)   # busy P1 lane listed nowhere


# ------------------------------------------------------- no LLM / no network
def test_oversight_source_imports_no_llm_or_network_stack():
    assert OVERSIGHT_SRC.is_file(), f"missing {OVERSIGHT_SRC}"
    source = OVERSIGHT_SRC.read_text(encoding="utf-8")
    hits = sorted({m.group(0) for m in BANNED.finditer(source)})
    assert hits == [], f"oversight.py must stay stdlib-only, found {hits}"


# ------------------------------------------------------------- systemd units
def test_oversight_timer_fires_every_30_minutes():
    timer = SYSTEMD / "plangraph-oversight-check.timer"
    assert timer.is_file(), f"missing {timer}"
    text = timer.read_text(encoding="utf-8")
    assert re.search(r"(?m)^OnCalendar=.*\*:0/30", text), \
        "the oversight timer must fire on the every-30-minute mark (*:0/30)"


def test_oversight_service_execstart_names_oversight_check():
    service = SYSTEMD / "plangraph-oversight-check.service"
    assert service.is_file(), f"missing {service}"
    text = service.read_text(encoding="utf-8")
    assert re.search(r"(?m)^ExecStart=.*\boversight-check\b", text), \
        "ExecStart must run the `oversight-check` cli verb"
    assert re.search(r"(?m)^Type=oneshot\s*$", text), "a scheduled check is oneshot"


# ---------------------------------------------------------- oversight-check
def write_config(w, tmp_path):
    """A mode-600 ``keeper.json`` over the World db (the local-verb pattern)."""
    snapshot = tmp_path / "plan.snapshot.json"
    snapshot.write_text(json.dumps(w.store.snapshot(w.p("router"))),
                        encoding="utf-8")
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(w.path), "snapshot": str(snapshot)}),
                   encoding="utf-8")
    os.chmod(cfg, 0o600)
    return cfg


def test_oversight_check_writes_both_digest_files_and_exits_0(
        w, tmp_path, monkeypatch, capsys):
    import plangraph.keeper.cli_oversight as cli_oversight  # noqa: F401
    from plangraph.keeper import cli
    assert "oversight-check" in cli.HANDLERS               # registered like `check`
    key = seed_claimed(w, ("A", ()), priority=1)[0]
    w.clock.advance(25 * HOUR)
    cfg = write_config(w, tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PG_CONFIG", str(cfg))
    monkeypatch.delenv("PG_URL", raising=False)
    monkeypatch.delenv("PG_TOKEN_FILE", raising=False)
    code = cli.main(["--config", str(cfg), "oversight-check",
                     "--out-dir", str(out)])
    assert code == 0
    payload = (out / "digest.json").read_bytes()
    recorded = re.search(r"[0-9a-f]{64}",
                         (out / "digest.sha256").read_text(encoding="utf-8"))
    assert recorded, "digest.sha256 must record the sha256 hex digest"
    assert recorded.group(0) == hashlib.sha256(payload).hexdigest()
    findings = json.loads(payload)
    assert findings and all(FINDING_KEYS <= set(f) for f in findings)
    assert any(f["key"] == key for f in findings)
    printed = capsys.readouterr().out
    assert re.search(
        rf"oversight: \d+ findings, \d+ new alarms, sha256 {recorded.group(0)}\b",
        printed)


# ---------------------------------------- cli-level R4 with a real git repo
def _git(repo, *args, env=None):
    """Run git in *repo* for the adapter test (test-side, never the module)."""
    import subprocess
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=full_env).stdout


def _lane_repo(tmp_path):
    """A repo with an old ``origin/main`` and an old, one-commit-ahead lane ref."""
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    old = {"GIT_AUTHOR_DATE": "2020-01-01T00:00:00 +0000",
           "GIT_COMMITTER_DATE": "2020-01-01T00:00:00 +0000"}
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", "base", env=old)
    _git(repo, "checkout", "-q", "-b", "lane/demo-a")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", "lane work", env=old)
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "update-ref", "refs/remotes/origin/lane/demo-a",
         "refs/heads/lane/demo-a")
    return repo


# ---------------------- cli-level R4 skip: no repo means R4 did not run
def test_oversight_check_with_no_configured_repo_keeps_open_lane_idle_alarms(
        w, tmp_path, monkeypatch):
    """With no lane git adapter R4 never ran, so its open alarms stay open."""
    import plangraph.keeper.cli_oversight  # noqa: F401
    from plangraph.keeper import cli
    key = seed_claimed(w, ("A", ()), priority=1)[0]
    now = w.clock()
    w.conn.execute(
        "INSERT INTO projector_alarms(id, kind, workflow_id, key, detail,"
        " raised_at) VALUES((SELECT COALESCE(MAX(id),0)+1 FROM"
        " projector_alarms),?,?,?,?,?)",
        ("lane_idle", "oversight", key, f"R4:lane_idle:{key}: lane idle",
         w.store._iso(now)))
    w.conn.commit()
    w.clock.advance(25 * HOUR)
    cfg = write_config(w, tmp_path)           # no "repos": _lane_git() is None
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(tmp_path)
    code = cli.main(["--config", str(cfg), "oversight-check",
                     "--out-dir", str(out)])
    assert code == 0
    row = w.conn.execute(
        "SELECT cleared_at FROM projector_alarms"
        " WHERE workflow_id='oversight' AND kind='lane_idle' AND key=?",
        (key,)).fetchone()
    assert row is not None
    assert row["cleared_at"] is None, "a rule that did not run must not clear alarms"


def test_oversight_check_uses_a_real_lane_git_adapter(w, tmp_path, monkeypatch):
    """The cli hands R4 a `git(args) -> str` adapter over cfg.repos, not cfg."""
    import plangraph.keeper.cli_oversight  # noqa: F401
    from plangraph.keeper import cli
    key = seed(w, [("A", ())])[0]             # demo:A maps onto lane/demo-a
    repo = _lane_repo(tmp_path)
    snapshot = tmp_path / "plan.snapshot.json"
    snapshot.write_text(json.dumps(w.store.snapshot(w.p("router"))),
                        encoding="utf-8")
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(w.path), "snapshot": str(snapshot),
                               "repos": {"demo": str(repo)}}),
                   encoding="utf-8")
    os.chmod(cfg, 0o600)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(tmp_path)
    code = cli.main(["--config", str(cfg), "oversight-check",
                     "--out-dir", str(out), "--no-alarms"])
    assert code == 0
    findings = json.loads((out / "digest.json").read_bytes())
    lanes = [f for f in findings if f["rule"] == "R4"]
    assert [(f["kind"], f["key"]) for f in lanes] == [("lane_idle", key)]
    assert "ahead of main" in lanes[0]["detail"]
