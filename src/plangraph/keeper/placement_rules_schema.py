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

# C3 owns PostgreSQL migration and row-preserving rollback.
PG = ""
