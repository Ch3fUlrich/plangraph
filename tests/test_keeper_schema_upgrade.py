"""A schema-v9 SQLite keeper database (no design-3 tables) upgrades when it is opened."""
import os
import re
import sqlite3

import pytest

from keeper_util import PRINCIPALS, Clock, defn
from plangraph.keeper import auth, db as kdb, events as keeper_events, schema
from plangraph.keeper.store import Store

NEW = ("cards", "attempts", "decisions", "questions", "answers", "budget")
#: The RT-P1-EXEC-OWNERSHIP columns on ``items`` (SQLite v13, Postgres migration 7):
#: the owner triple plus -- sq5 -- the owner principal (fix 1) and the retired-run
#: fence (fix 2), both installed by the same presence-based v13 upgrade path.
EXEC_COLUMNS = ("exec_workflow_id", "exec_run_id", "exec_attempt",
                "exec_principal", "exec_retired_runs")

#: Postgres shape (CI only): ``PG_DSN`` names the service container.
PG_DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN,
                              reason="PG_DSN not set (Postgres service container)")


def test_old_cards_gain_check_columns_idempotently(tmp_path):
    path = tmp_path / "keeper.db"
    raw = kdb.open_db(path, create=True)
    tokens = {n: auth.add_principal(raw, n, role, projects, family=fam)
              for n, role, projects, fam in PRINCIPALS}
    router = auth.authenticate(raw, tokens["router"])
    store = Store(raw, clock=Clock())
    store.create_items(router, [defn("A", []), defn("GOAL", ["A"], kind="goal")])
    store.put_card(router, "demo:A", {
        "outcome": "survive upgrade", "check_kind": "regression",
        "check_reads": ["tests/existing.py"],
    })
    raw.close()
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("ALTER TABLE cards DROP COLUMN check_kind")
    raw.execute("ALTER TABLE cards DROP COLUMN check_reads")
    raw.close()
    for _ in range(2):
        conn = kdb.open_db(path)
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(cards)")}
        assert cols["check_kind"][4] == "'change'"
        assert cols["check_reads"][4] == "'[]'"
        card = Store(conn, clock=Clock()).get_card(
            auth.authenticate(conn, tokens["router"]), "demo:A")
        assert card["outcome"] == "survive upgrade"
        assert card["check_kind"] == "change"
        assert card["check_reads"] == []
        conn.close()


def make_v9(path):
    """Build a database exactly as schema v9 installed it: today's DDL minus the design-3 part."""
    assert schema.DESIGN3_DDL in schema.SCHEMA
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.executescript(schema.SCHEMA.replace(schema.DESIGN3_DDL, ""))
    raw.close()


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v9_database_gains_the_design3_tables_at_open(tmp_path):
    path = tmp_path / "keeper.db"
    make_v9(path)
    raw = sqlite3.connect(str(path))
    assert not set(NEW) & tables(raw)
    raw.close()
    conn = kdb.open_db(path)                       # create=False: the upgrade path
    assert set(NEW) <= tables(conn)
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for table in ("attempts", "decisions", "answers"):
        assert {f"trg_{table}_append_only_update", f"trg_{table}_append_only_delete"} <= triggers
    conn.close()
    kdb.open_db(path).close()                      # idempotent


def test_upgraded_database_keeps_its_chain_and_works(tmp_path):
    path = tmp_path / "keeper.db"
    old = kdb.open_db(path, create=True)
    clock = Clock()
    tokens = {n: auth.add_principal(old, n, role, projects, family=fam)
              for n, role, projects, fam in PRINCIPALS}
    router = auth.authenticate(old, tokens["router"])
    store = Store(old, clock=clock)
    store.create_items(router, [defn("A", [], kind="lane"), defn("GOAL", ["A"], kind="goal")])
    head = keeper_events.read_events(old)[-1]
    old.close()
    # strip the design-3 part to simulate the v9 file, then reopen with current code
    raw = sqlite3.connect(str(path), isolation_level=None)
    for table in ("answers", "questions", "attempts", "decisions", "cards", "budget"):
        raw.execute(f"DROP TABLE {table}")
    raw.close()
    conn = kdb.open_db(path)
    assert set(NEW) <= tables(conn)
    assert keeper_events.read_events(conn)[-1]["hash"] == head["hash"]
    store = Store(conn, clock=clock)
    router = auth.authenticate(conn, tokens["router"])
    store.put_card(router, "demo:A", {"outcome": "o", "window_class": "1m"})
    store.set_state(router, "demo:A", "parked", "upgrade check")
    assert keeper_events.verify_chain(conn) == []
    assert keeper_events.verify_integrity(conn) == []
    conn.close()


def test_schema_version_is_thirteen():
    assert schema.SCHEMA_VERSION == 13


def test_real_v12_missing_exec_columns_upgrades_with_rows_and_chain(tmp_path):
    path = tmp_path / "v12.db"
    conn = kdb.open_db(path, create=True)
    token = auth.add_principal(conn, "router", "router", [])
    store = Store(conn, clock=Clock())
    store.create_items(auth.authenticate(conn, token),
                       [defn("A", [], kind="lane"), defn("GOAL", ["A"], kind="goal")])
    before = dict(store._item("demo:A"))
    chain = keeper_events.read_events(conn)
    conn.close()
    raw = sqlite3.connect(str(path))
    for column in ("exec_workflow_id", "exec_run_id", "exec_attempt"):
        raw.execute(f"ALTER TABLE items DROP COLUMN {column}")
        before.pop(column)
    raw.execute("PRAGMA user_version=12")
    raw.commit()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 12
    assert not {"exec_workflow_id", "exec_run_id", "exec_attempt"} & {
        row[1] for row in raw.execute("PRAGMA table_info(items)")}
    raw.close()
    conn = kdb.open_db(path)
    # Keeper versions are code-level SCHEMA_VERSION, not PRAGMA user_version:
    # production upgrades detect missing columns without reading/writing the pragma.
    assert schema.SCHEMA_VERSION == 13
    row = dict(Store(conn, clock=Clock())._item("demo:A"))
    for column in ("exec_workflow_id", "exec_run_id", "exec_attempt"):
        assert column in row and row.pop(column) is None
    assert row == before
    assert keeper_events.read_events(conn) == chain
    assert keeper_events.verify_chain(conn) == []
    assert keeper_events.verify_integrity(conn) == []
    conn.close()


def make_main_jev_schema(path):
    """Build a database exactly as **main's** schema installed it (#96 RT-P1-JEV-SHADOW,
    merged as 0f588e1d): the merged DDL minus the exec-ownership columns this lane
    appends to ``items`` -- jev shadow present, exec columns absent."""
    exec_ddl = ("    exec_workflow_id TEXT,\n    exec_run_id      TEXT,\n"
                "    exec_attempt     INTEGER,\n    exec_principal   TEXT,\n"
                "    exec_retired_runs TEXT,\n")
    assert exec_ddl in schema.SCHEMA
    assert schema.JEV_DDL in schema.SCHEMA
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.executescript(schema.SCHEMA.replace(exec_ddl, ""))
    raw.close()


def test_v13_db_missing_owner_principal_and_retired_fence_upgrades(tmp_path):
    """sq5 fix 1/2: main's shipped v13 (exec triple only) gains the owner-principal
    column and the retired-run fence column when this code opens it -- rows and the
    event chain survive, and both columns read NULL."""
    path = tmp_path / "v13-main.db"
    conn = kdb.open_db(path, create=True)
    token = auth.add_principal(conn, "router", "router", [])
    store = Store(conn, clock=Clock())
    store.create_items(auth.authenticate(conn, token),
                       [defn("A", [], kind="lane"), defn("GOAL", ["A"], kind="goal")])
    before = dict(store._item("demo:A"))
    chain = keeper_events.read_events(conn)
    conn.close()
    raw = sqlite3.connect(str(path))
    for column in ("exec_principal", "exec_retired_runs"):
        raw.execute(f"ALTER TABLE items DROP COLUMN {column}")
        before.pop(column)
    raw.commit()
    assert not {"exec_principal", "exec_retired_runs"} & {
        row[1] for row in raw.execute("PRAGMA table_info(items)")}
    raw.close()
    conn = kdb.open_db(path)
    row = dict(Store(conn, clock=Clock())._item("demo:A"))
    for column in ("exec_principal", "exec_retired_runs"):
        assert column in row and row.pop(column) is None
    assert row == before
    assert keeper_events.read_events(conn) == chain
    assert keeper_events.verify_chain(conn) == []
    conn.close()


def items_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(items)")}


def test_main_shaped_jev_db_gains_exec_columns_and_keeps_rows_and_chain(tmp_path):
    """A db created by main's schema (jev table, no exec columns) opens with the merged
    code: the exec columns appear, and the rows, the jev row and the event chain survive."""
    path = tmp_path / "main-jev.db"
    make_main_jev_schema(path)
    raw = sqlite3.connect(str(path))
    assert "jev_shadow" in tables(raw)
    assert not set(EXEC_COLUMNS) & items_columns(raw)
    raw.close()
    # The merged code opens main's shape: the upgrade path adds the exec columns.
    conn = kdb.open_db(path)
    assert set(EXEC_COLUMNS) <= items_columns(conn)
    assert "jev_shadow" in tables(conn)
    token = auth.add_principal(conn, "router", "router", [])
    router = auth.authenticate(conn, token)
    store = Store(conn, clock=Clock())
    store.create_items(router, [defn("A", [], kind="lane"), defn("GOAL", ["A"], kind="goal")])
    store.put_card(router, "demo:A", {"outcome": "o", "window_class": "1m"})
    store.set_state(router, "demo:A", "parked", "merge check")
    store.jev_record(router, {"card": "demo:A", "kind": "harness", "rule_answer": "rules",
                              "jev_answer": "jev", "status": "ok"})
    before = dict(store._item("demo:A"))
    chain = keeper_events.read_events(conn)
    conn.close()
    # Hand the same file back at main's shape *with its data*: the columns never shipped
    # there, so drop them again and let the merged code reopen it.
    raw = sqlite3.connect(str(path), isolation_level=None)
    for column in EXEC_COLUMNS:
        raw.execute(f"ALTER TABLE items DROP COLUMN {column}")
        before.pop(column)
    assert "jev_shadow" in tables(raw)
    assert not set(EXEC_COLUMNS) & items_columns(raw)
    raw.close()
    conn = kdb.open_db(path)
    assert set(EXEC_COLUMNS) <= items_columns(conn)
    row = dict(Store(conn, clock=Clock())._item("demo:A"))
    for column in EXEC_COLUMNS:
        assert column in row and row.pop(column) is None
    assert row == before
    assert [dict(r) for r in conn.execute(
        "SELECT key, kind, rule_answer, jev_answer FROM jev_shadow")] == [
        {"key": "demo:A", "kind": "harness", "rule_answer": "rules", "jev_answer": "jev"}]
    assert keeper_events.read_events(conn) == chain
    assert keeper_events.verify_chain(conn) == []
    assert keeper_events.verify_integrity(conn) == []
    conn.close()
    kdb.open_db(path).close()                       # idempotent


@needs_pg
def test_pg_db_at_migration_6_jev_upgrades_to_7(monkeypatch, tmp_path):
    """A Postgres schema at migration 6 (main's jev shape, no exec columns) upgrades to 7."""
    import uuid

    import psycopg

    from plangraph.keeper import db_pg, schema_pg

    name = "t_schemaup_" + uuid.uuid4().hex[:12]
    raw = psycopg.connect(PG_DSN, autocommit=True, prepare_threshold=None)

    def columns():
        return {row[0] for row in raw.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema=%s AND table_name='items'", (name,))}

    def jev_tables():
        return {row[0] for row in raw.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema=%s", (name,))}

    try:
        full = schema_pg.MIGRATIONS
        assert [m.version for m in full] == list(range(1, schema_pg.LATEST + 1))
        # main's shipped shape: migrations 1..6 (jev_shadow in 6), exec columns not yet.
        monkeypatch.setattr(schema_pg, "MIGRATIONS", full[:6])
        assert db_pg.migrate(raw, name) == [1, 2, 3, 4, 5, 6]
        assert "jev_shadow" in jev_tables()
        assert not set(EXEC_COLUMNS) & columns()
        # Merged code needs migration 7: opening the old schema is refused first ...
        path = tmp_path / "pg-jev.db"
        with pytest.raises(RuntimeError, match="code needs 7"):
            kdb.open_db(path, create=False, backend="postgres", dsn=PG_DSN, schema=name)
        # ... then pg admin migrate runs 7 and the exec columns are there.
        monkeypatch.setattr(schema_pg, "MIGRATIONS", full)
        assert db_pg.migrate(raw, name) == [7]
        assert set(EXEC_COLUMNS) <= columns()
        assert "jev_shadow" in jev_tables()
        assert [row[0] for row in raw.execute(
            "SELECT version FROM schema_migrations ORDER BY version")] == list(range(1, 8))
        conn = kdb.open_db(path, create=False, backend="postgres", dsn=PG_DSN, schema=name)
        conn.close()
    finally:
        raw.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        raw.close()


def test_open_without_create_still_refuses_a_non_keeper_file(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    with pytest.raises(RuntimeError):
        kdb.open_db(path)


def test_upgrade_oversight_triggers_atomic_transaction(tmp_path, monkeypatch):
    """db.py Finding 3: guard drop loop and trigger create run in one BEGIN IMMEDIATE transaction."""
    path = tmp_path / "upgrade_atomic.db"
    conn = kdb.open_db(path, create=True)
    conn.close()

    executed_statements = []
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            executed_statements.append((sql.strip(), self.in_transaction))
            return super().execute(sql, *a, **kw)

    def wrapped_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", wrapped_connect)
    conn = kdb.open_db(path, create=False)
    conn.close()

    drop_calls = [item for item in executed_statements if item[0].startswith("DROP TRIGGER IF EXISTS trg_oversight")]
    expected_trigger_count = len(re.findall(r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", schema.OVERSIGHT_DDL))
    assert len(drop_calls) == expected_trigger_count
    for stmt, in_tx in drop_calls:
        assert in_tx is True, f"Statement {stmt} was executed outside of a transaction!"

    create_calls = [item for item in executed_statements if item[0].upper().startswith("CREATE ")]
    expected_create_stmts = [
        s for s in kdb._split_statements(schema.OVERSIGHT_DDL)
        if s.upper().startswith("CREATE ")
    ]
    assert len(create_calls) == len(expected_create_stmts)
    for stmt, in_tx in create_calls:
        assert in_tx is True, f"Statement {stmt} was executed outside of a transaction!"

    create_trigger_calls = [
        item for item in executed_statements
        if re.match(r"^CREATE\s+TRIGGER", item[0], re.IGNORECASE)
    ]
    assert len(create_trigger_calls) == expected_trigger_count
    for stmt, in_tx in create_trigger_calls:
        assert in_tx is True, f"Statement {stmt} was executed outside of a transaction!"


def test_concurrent_update_oversight_ops_blocked_during_upgrade(tmp_path, monkeypatch):
    """B2: a second connection's UPDATE oversight_ops during upgrade is refused/blocked."""
    path = tmp_path / "concurrent_upgrade.db"
    conn = kdb.open_db(path, create=True)
    conn.execute(
        "INSERT INTO oversight_ops (ts, token_id, principal, generation, batch_ref, op_index, op, outcome) "
        "VALUES ('2026-01-01T00:00:00Z', 1, 'p1', 1, 'b1', 0, 'create', 'applied')"
    )
    conn.close()

    second_conn_attempted = False
    second_conn_error = None
    real_connect = sqlite3.connect

    class InterceptConnection(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            nonlocal second_conn_attempted, second_conn_error
            if sql.strip().startswith("DROP TRIGGER IF EXISTS trg_oversight"):
                assert self.in_transaction is True
                if not second_conn_attempted:
                    second_conn_attempted = True
                    conn2 = real_connect(str(path), timeout=0.01)
                    try:
                        conn2.execute("UPDATE oversight_ops SET outcome = 'denied' WHERE id = 1")
                    except sqlite3.OperationalError as exc:
                        second_conn_error = exc
                    finally:
                        conn2.close()
            return super().execute(sql, *a, **kw)

    def wrapped_connect(*args, **kwargs):
        kwargs["factory"] = InterceptConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", wrapped_connect)
    conn = kdb.open_db(path, create=False)
    conn.close()

    assert second_conn_attempted is True
    assert second_conn_error is not None
    assert "locked" in str(second_conn_error).lower() or "busy" in str(second_conn_error).lower()

    conn_after = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.IntegrityError, match="oversight_ops is append-only"):
        conn_after.execute("UPDATE oversight_ops SET outcome = 'denied' WHERE id = 1")
    conn_after.close()


def test_split_statements_two_statements_on_one_line():
    """B1: two statements on one line split into distinct executable statements."""
    sql = "CREATE TABLE t1 (a INT); CREATE TABLE t2 (b TEXT);"
    stmts = kdb._split_statements(sql)
    assert len(stmts) == 2
    assert stmts == ["CREATE TABLE t1 (a INT);", "CREATE TABLE t2 (b TEXT);"]
    conn = sqlite3.connect(":memory:")
    for stmt in stmts:
        conn.execute(stmt)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"t1", "t2"} <= tables
    conn.close()


def test_split_statements_trigger_body_with_inner_semicolons():
    """B1: trigger body with inner semicolons (BEGIN...END) is preserved as one statement."""
    sql = """
    CREATE TABLE src (id INT);
    CREATE TABLE dst1 (id INT);
    CREATE TABLE dst2 (id INT);
    CREATE TRIGGER trg_multi AFTER INSERT ON src
    BEGIN
        INSERT INTO dst1 VALUES (NEW.id);
        INSERT INTO dst2 VALUES (NEW.id);
    END;
    INSERT INTO src VALUES (42);
    """
    stmts = kdb._split_statements(sql)
    assert len(stmts) == 5
    conn = sqlite3.connect(":memory:")
    for stmt in stmts:
        conn.execute(stmt)
    assert conn.execute("SELECT COUNT(*) FROM dst1").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dst2").fetchone()[0] == 1
    conn.close()


def test_split_statements_refuses_pragma_and_vacuum_in_transaction(tmp_path, monkeypatch):
    """B1: refuse PRAGMA/VACUUM inside the transaction with a clear error."""
    with pytest.raises(ValueError, match="disallowed statement in transaction: PRAGMA"):
        kdb._split_statements("PRAGMA foreign_keys = OFF;")

    with pytest.raises(ValueError, match="disallowed statement in transaction: VACUUM"):
        kdb._split_statements("VACUUM;")

    with pytest.raises(ValueError, match="disallowed statement in transaction: PRAGMA"):
        kdb._split_statements("-- leading comment\n  PRAGMA journal_mode = WAL;")

    path = tmp_path / "pragma_upgrade.db"
    kdb.open_db(path, create=True).close()

    monkeypatch.setattr(kdb, "OVERSIGHT_DDL", "PRAGMA user_version = 99;")
    with pytest.raises(ValueError, match="disallowed statement in transaction: PRAGMA"):
        kdb.open_db(path, create=False)

    monkeypatch.setattr(kdb, "OVERSIGHT_DDL", "VACUUM;")
    with pytest.raises(ValueError, match="disallowed statement in transaction: VACUUM"):
        kdb.open_db(path, create=False)


