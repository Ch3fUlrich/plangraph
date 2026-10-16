"""``pg admin copy-to-pg`` (W3): SQLite keeper snapshot -> empty Postgres keeper.

The dry-run, refusal and argument tests run on SQLite alone (also inside the Postgres CI
job, where the module name keeps it collected); the full ``--apply`` test needs ``PG_DSN``.
"""
import hashlib
import json
import os
import sqlite3

import pytest

from plangraph.keeper import cli, config, db as kdb, events as keeper_events
from keeper_util import World
from test_keeper_pg_parity import replay

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (Postgres service container)")

TABLES = ("events", "items", "evidence", "edges", "principals", "aliases", "run_tokens",
          "outbox_seen", "audit_log", "audit_findings",
          "cards", "attempts", "decisions", "questions", "answers", "budget")


@pytest.fixture
def source(tmp_path):
    """A seeded SQLite keeper file (closed, checkpointed) from the parity workload."""
    d = tmp_path / "src"
    d.mkdir()
    w = World(d, backend="sqlite")
    replay(w)
    w.close()
    return w.path


@pytest.fixture
def cfg_path(tmp_path):
    d = tmp_path / "target"
    d.mkdir()
    path = d / "keeper.json"
    path.write_text(json.dumps({"db": str(d / "keeper.db"), "snapshot": str(d / "plan.json"),
                                "bind": "127.0.0.1:0"}), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raw_counts(path):
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        have = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in TABLES if t in have}
        seq, digest = raw.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    finally:
        raw.close()
    return counts, (seq, digest)


def copy(cfg_path, *argv):
    return cli.main(["--config", str(cfg_path), "admin", "copy-to-pg", *argv])


def test_dry_run_reports_counts_and_head(source, cfg_path, tmp_path, capsys):
    before = sha(source)
    counts, head = raw_counts(source)
    assert counts["events"] > 10 and counts["cards"] and counts["attempts"] and counts["answers"]
    assert copy(cfg_path, "--dry-run", "--src", str(source)) == 0
    out = capsys.readouterr().out
    assert sha(source) == before                                  # the source is never written
    rows = {line.split()[0]: line.split()[1] for line in out.splitlines()
            if line.split() and line.split()[0] in counts}
    assert rows == {t: str(n) for t, n in counts.items()}
    assert f"seq={head[0]} hash={head[1]}" in out
    assert "source chain ok" in out
    assert str(tmp_path) not in out                               # no paths in the report


def test_dry_run_is_deterministic(source, cfg_path, capsys):
    assert copy(cfg_path, "--dry-run", "--src", str(source)) == 0
    first = capsys.readouterr().out
    assert copy(cfg_path, "--dry-run", "--src", str(source)) == 0
    assert capsys.readouterr().out == first


def test_dry_run_flags_a_tampered_chain(source, cfg_path, tmp_path, capsys):
    bad = tmp_path / "tampered.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(bad)
    src.backup(dst)
    src.close()
    dst.execute("DROP TRIGGER trg_events_append_only_update")
    dst.execute("UPDATE events SET payload = '{\"forged\": 1}' WHERE seq = 3")
    dst.commit()
    dst.close()
    assert copy(cfg_path, "--dry-run", "--src", str(bad)) != 0
    captured = capsys.readouterr()
    assert "hash_mismatch" in captured.err and "broken" in captured.err
    assert "source chain ok" not in captured.out


def test_missing_or_foreign_source_is_refused(cfg_path, tmp_path, capsys):
    assert copy(cfg_path, "--dry-run", "--src", str(tmp_path / "absent.db")) == 2
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    assert copy(cfg_path, "--dry-run", "--src", str(other)) == 2
    err = capsys.readouterr().err
    assert "not found" in err and "not a keeper database" in err and str(tmp_path) not in err


def test_dry_run_and_apply_are_mutually_exclusive(cfg_path, capsys):
    with pytest.raises(SystemExit) as both:
        copy(cfg_path, "--dry-run", "--apply")
    assert both.value.code == 2
    with pytest.raises(SystemExit) as neither:
        copy(cfg_path)
    assert neither.value.code == 2


def test_apply_without_dsn_is_a_clear_error(source, cfg_path, monkeypatch, capsys):
    monkeypatch.delenv("PG_DSN", raising=False)
    assert copy(cfg_path, "--apply", "--src", str(source)) == 2
    err = capsys.readouterr().err
    assert "DSN" in err and "PG_DSN" in err and "://" not in err


def test_apply_never_prints_the_dsn(source, cfg_path, monkeypatch, capsys):
    secret = "s3cr3t-pw"
    monkeypatch.setenv("PG_DSN", f"postgresql://keeper:{secret}@127.0.0.1:1/nope")
    assert copy(cfg_path, "--apply", "--src", str(source)) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err and "keeper:" not in captured.err


@needs_pg
def test_apply_copies_verbatim_and_refuses_a_second_run(source, cfg_path, capsys):
    from plangraph.keeper import db_pg
    cfg = config.load_config(cfg_path)
    counts, head = raw_counts(source)
    before = sha(source)
    try:
        assert copy(cfg_path, "--apply", "--src", str(source)) == 0
        out = capsys.readouterr().out
        assert "copied and verified" in out and sha(source) == before
        conn = kdb.open_db(cfg.db, backend="postgres", dsn=DSN)
        try:
            got = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}
            assert got == counts
            last = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            assert (last[0], last[1]) == head
            assert keeper_events.verify_chain(conn) == []
            # every events row is byte-identical to the source
            raw = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            want = [tuple(r) for r in raw.execute(
                "SELECT seq,ts,actor,role,model,key,type,payload,prev_hash,hash FROM events ORDER BY seq")]
            raw.close()
            have = [tuple(r) for r in conn.execute(
                "SELECT seq,ts,actor,role,model,key,type,payload,prev_hash,hash FROM events ORDER BY seq")]
            assert have == want
            # triggers are back on, and identity ids continue after the copied maximum
            with pytest.raises(Exception):
                conn.execute("UPDATE events SET type = 'x' WHERE seq = 1")
            conn.execute("INSERT INTO audit_log(ts,actor,action,ok) VALUES('t','a','probe',1)")
            top = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
            raw = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            src_top = raw.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
            raw.close()
            assert top == src_top + 1
        finally:
            conn.close()
        assert copy(cfg_path, "--apply", "--src", str(source)) == 2
        assert "not empty" in capsys.readouterr().err
    finally:
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as raw:
            raw.execute(f'DROP SCHEMA IF EXISTS "{db_pg.derive_schema(cfg.db)}" CASCADE')
