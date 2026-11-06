"""C2: storage in the existing keeper, with no default/seed overrides.

The transaction adapter (C4) stores exact canonical RuleEdit JSON in edit,
including expected_revision and rollback_of. Rollback appends a forward event;
this ledger is independent of item events. No secrets or host metrics belong here.
"""

SQLITE = """
CREATE TABLE IF NOT EXISTS placement_rule_revision (
    id INTEGER PRIMARY KEY CHECK (id=1),
    revision INTEGER NOT NULL CHECK (revision>=0)
);
CREATE TABLE IF NOT EXISTS placement_rule_values (
    scope TEXT NOT NULL CHECK (scope IN ('fleet','host')),
    host TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scope,host,key),
    CHECK ((scope='fleet' AND host='') OR (scope='host' AND length(host)>0))
);
CREATE TABLE IF NOT EXISTS placement_rule_audit (
    revision INTEGER PRIMARY KEY CHECK (revision>0),
    actor TEXT NOT NULL,
    edit TEXT NOT NULL,
    previous TEXT,
    ts TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_placement_rule_audit_no_update
BEFORE UPDATE ON placement_rule_audit
BEGIN SELECT RAISE(ABORT, 'placement_rule_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_placement_rule_audit_no_delete
BEFORE DELETE ON placement_rule_audit
BEGIN SELECT RAISE(ABORT, 'placement_rule_audit is append-only'); END;
"""


PG = '''
DO $$ BEGIN
    IF to_regclass('placement_rule_revision_retired_v11') IS NOT NULL
       OR to_regclass('placement_rule_values_retired_v11') IS NOT NULL
       OR to_regclass('placement_rule_audit_retired_v11') IS NOT NULL THEN
        RAISE EXCEPTION 'placement_rules_retired_ledger_exists';
    END IF;
END $$;

CREATE TABLE placement_rule_revision (
    id BIGINT PRIMARY KEY CHECK (id=1),
    revision BIGINT NOT NULL CHECK (revision>=0)
);
CREATE TABLE placement_rule_values (
    scope TEXT COLLATE "C" NOT NULL CHECK (scope IN ('fleet','host')),
    host TEXT COLLATE "C" NOT NULL,
    key TEXT COLLATE "C" NOT NULL,
    value TEXT COLLATE "C" NOT NULL,
    PRIMARY KEY (scope,host,key),
    CHECK ((scope='fleet' AND host='') OR (scope='host' AND length(host)>0))
);
CREATE TABLE placement_rule_audit (
    revision BIGINT PRIMARY KEY CHECK (revision>0),
    actor TEXT COLLATE "C" NOT NULL,
    edit TEXT COLLATE "C" NOT NULL,
    previous TEXT COLLATE "C",
    ts TEXT COLLATE "C" NOT NULL
);
CREATE TRIGGER trg_placement_rule_audit_no_update BEFORE UPDATE ON placement_rule_audit
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('placement_rule_audit is append-only');
CREATE TRIGGER trg_placement_rule_audit_no_delete BEFORE DELETE ON placement_rule_audit
    FOR EACH ROW EXECUTE FUNCTION pgk_reject('placement_rule_audit is append-only');
CREATE TRIGGER trg_placement_rule_audit_no_truncate BEFORE TRUNCATE ON placement_rule_audit
    FOR EACH STATEMENT EXECUTE FUNCTION pgk_reject('placement_rule_audit is append-only');
'''

TABLES = ("placement_rule_revision", "placement_rule_values", "placement_rule_audit")


def rollback_pg(conn) -> None:
    """Retire with raw psycopg migrate-role connection and approved search_path.

    Operators keep application writers stopped throughout rollback and restore.
    PgConnection's guard deliberately denies the required LOCK and DDL commands.
    """
    from psycopg import sql
    from .db_pg import _MIGRATE_LOCK

    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(current_schema()))",
                     (_MIGRATE_LOCK,))
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        migration_table = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=%s "
            "AND table_name='schema_migrations'", (schema,)).fetchone()
        if not migration_table:
            raise ValueError("placement_rules_keeper_schema_required")
        indexes = [row[0] for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname=%s "
            "AND tablename=ANY(%s)", (schema, list(TABLES))).fetchall()]
        for name in (*TABLES, *indexes):
            if conn.execute("SELECT to_regclass(%s)",
                            (name + "_retired_v11",)).fetchone()[0]:
                raise ValueError("placement_rules_retired_names_exist")
        if not conn.execute(sql.SQL("SELECT 1 FROM {}.schema_migrations WHERE version=11")
                            .format(sql.Identifier(schema))).fetchone():
            raise ValueError("placement_rules_migration_11_required")
        conn.execute("LOCK TABLE placement_rule_revision, placement_rule_values, "
                     "placement_rule_audit IN ACCESS EXCLUSIVE MODE")
        for name in indexes:
            conn.execute(sql.SQL("ALTER INDEX {} RENAME TO {}").format(
                sql.Identifier(name), sql.Identifier(name + "_retired_v11")))
        for name in TABLES:
            conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                sql.Identifier(name), sql.Identifier(name + "_retired_v11")))
        conn.execute("DELETE FROM schema_migrations WHERE version=11")
