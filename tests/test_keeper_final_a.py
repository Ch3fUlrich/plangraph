"""Final hardening pass A: principal re-check, admin scaffolding, logging, anchors.

Four independent fixes, kept in one file so the change can be reviewed as one
unit:

* (A) ``Store._check_principal`` refuses a principal with no row, except the
      synthetic ``system``/``system`` identity the API reaper uses.
* (B) the dead ``_admin_ctx`` bypass is gone; the principals trigger allows
      only a bare ``active`` 1 -> 0 revoke, and DELETE always aborts.
* (C) malformed input is logged server-side (verb + key only, never the body).
* (D) the evidence regexes anchor at the end and reject non-ASCII digits.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from keeper_util import Clock, World, seed, to_claimed
from plangraph.keeper import auth, db as kdb
from plangraph.keeper.auth import Principal
from plangraph.keeper.evidence_ci import CIAdapter
from plangraph.keeper.evidence_decisions import DECISION_LINE_RE, DecisionsAdapter
from plangraph.keeper.evidence_runs import RunRecordAdapter
from plangraph.keeper.evidence_types import valid_commit_ref
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def last_audit(w):
    return w.conn.execute(
        "SELECT ok,code FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()


# ============================================================= (A) principals

def test_principal_without_row_is_refused(w):
    """A token resolved to a name with no row must not keep acting."""
    (key,) = seed(w, [("A", [])])
    ghost = Principal("ghost", "worker", ["demo"], None)
    with pytest.raises(Refused) as exc:
        w.store.claim(ghost, key)
    assert exc.value.code == "forbidden"
    assert last_audit(w)["code"] == "forbidden"


@pytest.mark.parametrize("name,role", [
    ("system", "worker"),      # right name, wrong role
    ("other", "system"),       # right role, wrong name
])
def test_principal_without_row_only_system_system_allowed(w, name, role):
    (key,) = seed(w, [("A", [])])
    impostor = Principal(name, role, [], None)
    with pytest.raises(Refused) as exc:
        w.store.reap(impostor)
    assert exc.value.code == "forbidden"


def test_synthetic_system_without_row_may_reap(tmp_path):
    """The API reaper's identity works even with no stored principal row."""
    conn = kdb.open_db(tmp_path / "bare.db", create=True)
    try:
        store = Store(conn, clock=Clock())
        synthetic = Principal("system", "system", [], None)
        assert store.reap(synthetic) == {"expired": [], "overdue": []}
        assert last_audit_row(conn)["code"] is None
    finally:
        conn.close()


def last_audit_row(conn):
    return conn.execute(
        "SELECT ok,code FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()


# ===================================================== (B) admin scaffolding

def test_admin_ctx_table_is_gone(w):
    row = w.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='_admin_ctx'").fetchone()
    assert row is None


def test_principals_delete_aborts(w):
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("DELETE FROM principals WHERE name='workerA'")
    still = w.conn.execute(
        "SELECT 1 FROM principals WHERE name='workerA'").fetchone()
    assert still is not None


def test_principals_bare_revoke_allowed(w):
    w.store.conn.execute("UPDATE principals SET active=0 WHERE name='workerA'")
    row = w.conn.execute(
        "SELECT active FROM principals WHERE name='workerA'").fetchone()
    assert row["active"] == 0


@pytest.mark.parametrize("sql", [
    # revoke plus any second edit is not a bare revoke
    "UPDATE principals SET active=0, family='teamZ' WHERE name='workerB'",
    "UPDATE principals SET active=0, role='reader' WHERE name='workerB'",
    "UPDATE principals SET active=0, projects='[]' WHERE name='workerB'",
    # reactivating is never allowed
    "UPDATE principals SET active=1 WHERE name='workerA'",
])
def test_principals_non_bare_update_aborts(w, sql):
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(sql)


def test_principals_insert_still_allowed(w):
    token = auth.add_principal(w.conn, "newcomer", "reader", [], family=None)
    assert auth.authenticate(w.conn, token).name == "newcomer"


# ================================================= (C) malformed-input logging

SECRET = "sk-live-DEADBEEF0123456789abcdef"


def test_malformed_input_is_logged_without_body(w, caplog):
    """The traceback is logged, but the request body never reaches the log."""
    with caplog.at_level(logging.ERROR, logger="plangraph.keeper"):
        with pytest.raises(Refused) as exc:
            w.store.create_items(w.p("router"), [SECRET])
    assert exc.value.code == "validation"
    assert last_audit(w)["code"] == "validation"
    text = caplog.text
    assert "malformed input" in text
    assert "create_items" in text
    assert SECRET not in text


def test_malformed_logger_name_and_message(w, caplog):
    with caplog.at_level(logging.ERROR, logger="plangraph.keeper"):
        with pytest.raises(Refused):
            w.store.create_items(w.p("router"), [SECRET])
    records = [r for r in caplog.records if r.name == "plangraph.keeper"]
    assert records, "expected a plangraph.keeper log record"
    record = records[-1]
    assert record.exc_info is not None            # traceback, server-side
    assert "create_items" in record.getMessage()
    assert SECRET not in record.getMessage()


# ============================================================ (D) anchors

@pytest.mark.parametrize("sha", ["a" * 40 + "\n", "a" * 64 + "\n",
                                 "a" * 40 + "\r\n"])
def test_valid_commit_ref_rejects_trailing_newline(sha):
    assert valid_commit_ref(sha) is False


def test_valid_commit_ref_still_accepts_clean_sha():
    assert valid_commit_ref("a" * 40) is True
    assert valid_commit_ref("0" * 64) is True


def test_run_id_rejects_trailing_newline():
    adapter = RunRecordAdapter("/runs")
    assert adapter.valid_run_id("run-1\n") is False
    assert adapter.valid_run_id("run-1") is True


def test_ci_slug_rejects_trailing_newline(monkeypatch):
    calls: list = []
    from plangraph.keeper import evidence_ci
    monkeypatch.setattr(evidence_ci.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        CIAdapter().check_runs("owner/repo\n", "a" * 40)
    assert calls == []


def test_decision_line_rejects_non_ascii_digits(tmp_path):
    doc = tmp_path / "DECISIONS.md"
    doc.write_text(
        "# Decisions\n\n"
        "| D-209 | real |\n"
        "| D-\u0661\u0662\u0663 | arabic-indic digits |\n",
        encoding="utf-8",
    )
    assert DECISION_LINE_RE.match("| D-\u0661\u0662\u0663 | x |") is None
    adapter = DecisionsAdapter(doc)
    assert adapter.decisions() == {"D-209"}
    assert adapter.has("D-123") is False
    assert adapter.has("D-\u0661\u0662\u0663") is False
