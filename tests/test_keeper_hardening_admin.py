"""Hardening tests for findings 8-10 of the adversarial keeper-store review.

Finding 8: malformed input -> Refused('validation') with an audit row; caps.
Finding 9: audit append-only, principals admin-only, write pragmas denied.
Finding 10: cross-project dep, add_principal list validation, stale principal,
            reviewer independence from BOTH claimant and delegate.

Findings 5-7 (lock/guard/fold-integrity) live in
``test_keeper_hardening_guards.py``; the original file was split to stay under
400 lines.
"""
import sqlite3

import pytest

from keeper_util import World, defn, seed, to_claimed
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import iso


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def last_audit(w):
    return w.conn.execute(
        "SELECT ok,code FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()


# =============================================================== finding 8

def test_malformed_batch_refused_and_audited(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), ["x"])
    assert exc.value.code == "validation"
    row = last_audit(w)
    assert row is not None and row["ok"] == 0 and row["code"] == "validation"


def test_non_string_dependency_refused(w):
    bad = defn("A")
    bad["depends_on"] = [5]
    items = [bad, defn("GOAL", kind="goal", deps=["A"])]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), items)
    assert exc.value.code == "validation"
    assert last_audit(w)["code"] == "validation"


@pytest.mark.parametrize("lease", [float("nan"), "abc", [1]])
def test_bad_lease_refused(w, lease):
    (key,) = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("workerA"), key, lease_s=lease)
    assert exc.value.code == "validation"
    assert last_audit(w)["code"] == "validation"


def test_edit_title_dict_refused(w):
    (key,) = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), key, {"title": {"x": 1}})
    assert exc.value.code == "validation"
    assert last_audit(w)["code"] == "validation"


@pytest.mark.parametrize("field,cap", [
    ("title", 200), ("note", 2000), ("done_criteria", 600),
])
def test_text_caps(w, field, cap):
    item = defn("A")
    item[field] = "x" * (cap + 1)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"),
                             [item, defn("GOAL", kind="goal", deps=["A"])])
    assert exc.value.code == "validation"


def test_batch_cap(w):
    items = [defn(f"A{i}") for i in range(501)]
    items.append(defn("GOAL", kind="goal", deps=[f"A{i}" for i in range(501)]))
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), items)
    assert exc.value.code == "validation"


def test_depends_on_cap(w):
    item = defn("A")
    item["depends_on"] = [f"demo:D{i}" for i in range(201)]
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [item])
    assert exc.value.code == "validation"


def test_lease_is_clamped(w):
    (key,) = seed(w, [("A", [])])
    w.store.claim(w.p("workerA"), key, lease_s=5)
    assert w.store._item(key)["lease_until"] == iso(w.clock() + 60)
    w.store.release(w.p("workerA"), key, 1)
    w.store.claim(w.p("workerA"), key, lease_s=10_000_000)
    assert w.store._item(key)["lease_until"] == iso(w.clock() + 86400)


def test_audit_auth_failure(w):
    w.store.audit_auth_failure("no bearer token")
    row = w.conn.execute(
        "SELECT actor,action,ok,code FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["actor"] == "?" and row["ok"] == 0 and row["code"] == "forbidden"


# =============================================================== finding 9

def test_audit_log_append_only(w):
    seed(w, [("A", [])])
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE audit_log SET ok=1 WHERE id=1")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("DELETE FROM audit_log")


def test_audit_findings_append_only(w):
    w.store.conn.execute(
        "INSERT INTO audit_findings(ts,key,code,msg) VALUES('t',NULL,'x','y')")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE audit_findings SET code='z' WHERE id=1")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("DELETE FROM audit_findings")


def test_principal_non_revoke_update_aborts(w):
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE principals SET role='router' WHERE name='workerA'")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(
            "UPDATE principals SET projects='[\"demo\",\"other\"]'"
            " WHERE name='workerA'")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE principals SET active=1 WHERE name='workerA'")


def test_principal_sql_revoke_allowed_and_token_dies(w):
    w.store.conn.execute("UPDATE principals SET active=0 WHERE name='workerA'")
    with pytest.raises(Refused) as exc:
        auth.authenticate(w.conn, w.tok("workerA"))
    assert exc.value.code == "forbidden"
    auth.revoke(w.conn, "workerB")


@pytest.mark.parametrize("sql", [
    "PRAGMA recursive_triggers=OFF",
    "PRAGMA foreign_keys=OFF",
    "PRAGMA trusted_schema=ON",
    "PRAGMA ignore_check_constraints=ON",
    "PRAGMA journal_mode=DELETE",
    "PRAGMA query_only=ON",
    "PRAGMA schema_version=12345",
    "PRAGMA writable_schema=ON",
])
def test_write_pragmas_denied(w, sql):
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(sql)


def test_read_pragmas_allowed(w):
    assert w.store.conn.execute("PRAGMA foreign_keys").fetchone() is not None
    assert w.store.conn.execute("PRAGMA recursive_triggers").fetchone() is not None


# =============================================================== finding 10

def test_add_principal_rejects_bare_string_projects(w):
    with pytest.raises(Refused) as exc:
        auth.add_principal(w.conn, "strproj", "worker", "demo")
    assert exc.value.code == "validation"


def test_add_principal_rejects_non_string_project_entries(w):
    with pytest.raises(Refused) as exc:
        auth.add_principal(w.conn, "badproj", "worker", ["demo", 5])
    assert exc.value.code == "validation"


def test_stale_principal_refused_after_revoke(w):
    principal = auth.authenticate(w.conn, w.tok("workerA"))
    auth.revoke(w.conn, "workerA")
    (key,) = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.claim(principal, key)
    assert exc.value.code == "forbidden"


def test_reviewer_must_differ_from_delegate_family(w):
    (key,) = seed(w, [("A", [])])
    token = auth.add_principal(w.conn, "reviewerB", "reviewer", ["demo"],
                               family="teamB")
    reviewer_b = auth.authenticate(w.conn, token)
    epoch = to_claimed(w, key)
    w.store.delegate(w.p("l1main"), key, "workerA")   # workerA is teamB
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), key, epoch)
    with pytest.raises(Refused) as exc:
        w.store.verdict(reviewer_b, key, True, "x")
    assert exc.value.code == "same_family"


def test_reviewer_unknown_delegate_family_fails_closed(w):
    (key,) = seed(w, [("A", [])])
    token = auth.add_principal(w.conn, "workerNoFam", "worker", ["demo"],
                               family=None)
    worker = auth.authenticate(w.conn, token)
    epoch = to_claimed(w, key)
    w.store.delegate(w.p("l1main"), key, worker)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), key, epoch)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), key, True, "x")
    assert exc.value.code == "family_unknown"
