"""SQL-level attack suite: locking triggers and the authorizer."""
import sqlite3

import pytest

from keeper_util import World, seed, to_done
from plangraph.keeper.db import lock_keeper
from plangraph.keeper.errors import KeeperLocked


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def locked_world(w, state):
    """A locked item that has its own dependency edge (so edge tests bite)."""
    keys = seed(w, [("P", []), ("X", ["P"])])
    to_done(w, keys[0])
    if state == "done":
        to_done(w, keys[1])
    else:
        w.store.cancel(w.p("router"), keys[1], "drop")
    return keys[1]


# ------------------------------------------------------------------- done

def test_update_done_row_aborts(w):
    key = locked_world(w, "done")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE items SET title='x' WHERE key=?", (key,))


def test_delete_done_row_aborts(w):
    key = locked_world(w, "done")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("DELETE FROM items WHERE key=?", (key,))


def test_insert_or_replace_done_row_aborts(w):
    key = locked_world(w, "done")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(
            "INSERT OR REPLACE INTO items SELECT * FROM items WHERE key=?", (key,))


def test_insert_event_for_done_aborts(w):
    key = locked_world(w, "done")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(
            "INSERT INTO events(ts,actor,role,model,key,type,payload,prev_hash,hash)"
            " VALUES('t','x','router',NULL,?,'item_defined','{}','','h')", (key,))


def test_insert_evidence_for_done_aborts(w):
    key = locked_world(w, "done")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(
            "INSERT INTO evidence(key,epoch,kind,ref,verified,detail,at)"
            " VALUES(?,1,'decision','{}',1,'x','t')", (key,))


def test_insert_edge_from_done_aborts(w):
    key = locked_world(w, "done")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(
            "INSERT INTO edges(src,dst,rel) VALUES(?,'demo:B','depends_on')", (key,))


def test_delete_edge_from_done_aborts(w):
    key = locked_world(w, "done")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("DELETE FROM edges WHERE src=?", (key,))


# -------------------------------------------------------------- cancelled

@pytest.mark.parametrize("statement,args", [
    ("UPDATE items SET title='x' WHERE key=?", ()),
    ("DELETE FROM items WHERE key=?", ()),
])
def test_cancelled_row_frozen(w, statement, args):
    key = locked_world(w, "cancelled")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(statement, (key, *args))


def test_insert_or_replace_cancelled_row_aborts(w):
    key = locked_world(w, "cancelled")
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(
            "INSERT OR REPLACE INTO items SELECT * FROM items WHERE key=?", (key,))


# ------------------------------------------------------------- append-only

def test_events_update_always_aborts(w):
    seed(w, [("A", [])])
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("UPDATE events SET payload='{}' WHERE seq=1")


def test_events_delete_always_aborts(w):
    seed(w, [("A", [])])
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("DELETE FROM events WHERE seq=1")


# ------------------------------------------------------------- authorizer

@pytest.mark.parametrize("sql", [
    "DROP TRIGGER trg_events_append_only_update",
    "PRAGMA writable_schema=ON",
    "ALTER TABLE items ADD COLUMN sneaky TEXT",
    "CREATE TABLE sneaky (x TEXT)",
    "ATTACH DATABASE '/tmp/sneaky.db' AS sneaky",
])
def test_ddl_and_writable_schema_denied(w, sql):
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute(sql)


def test_load_extension_denied(w):
    with pytest.raises(sqlite3.DatabaseError):
        w.store.conn.execute("SELECT load_extension('evil')")


def test_dml_still_allowed(w):
    (key,) = seed(w, [("A", [])])
    w.store.conn.execute("UPDATE items SET title='ok' WHERE key=?", (key,))
    assert w.store._item(key)["title"] == "ok"


# ------------------------------------------------------------ flock helper

def test_lock_keeper_is_singleton(tmp_path):
    path = tmp_path / "keeper.db"
    first = lock_keeper(path)
    try:
        with pytest.raises(KeeperLocked):
            lock_keeper(path)
    finally:
        first.release()
    second = lock_keeper(path)
    second.release()
