"""Behavioural Postgres coverage for the exec-owner fence (RT-P1 V3-A).

``test_keeper_pg_unit.py`` pins migration 6's DDL structurally and only SQLite
ran the real behaviour; this file applies migration 6 on a real Postgres
schema and then exercises an owner-fence call against it: a park carrying an
older attempt than the recorded owner must refuse ``exec_stale`` and write
nothing, exactly as on SQLite.  Needs a Postgres server: set ``PG_DSN`` (CI
provides one); skipped otherwise.
"""
import os

import pytest

from keeper_util import World, seed
from plangraph.keeper.errors import Refused

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (Postgres service container)")

#: The exec identity the router records first (attempt 2).
RUN2 = {"workflow_id": "wf", "run_id": "run", "attempt": 2}


def owner(w, key):
    row = w.conn.execute(
        "SELECT exec_workflow_id, exec_run_id, exec_attempt FROM items WHERE key=?",
        (key,)).fetchone()
    return tuple(row)


@needs_pg
def test_migration_6_applies_on_pg_and_park_refuses_an_older_attempt(tmp_path):
    d = tmp_path / "pgexec"
    d.mkdir()
    w = World(d, backend="postgres", dsn=DSN)
    try:
        # Migration 6 (the three exec ownership columns) was applied here.
        versions = [row[0] for row in w.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        assert 6 in versions, versions
        key, = seed(w, [("A", [])])
        # A run-carrying move records the owner at attempt 2 on Postgres ...
        w.store.set_state(w.p("router"), key, "running", run=("wf", "run", 2))
        assert owner(w, key) == ("wf", "run", 2)
        before = dict(w.store._item(key))
        # ... so a park from the same run at the older attempt is exec_stale
        # and leaves the card (owner included) untouched.
        with pytest.raises(Refused) as exc:
            w.store.worker_park(w.p("workerA"), {
                "card": key, "tier": "free", "exit_class": "check_fail",
                "workflow_id": "wf", "run_id": "run", "attempt": 1})
        assert exc.value.code == "exec_stale"
        assert dict(w.store._item(key)) == before
        assert owner(w, key) == ("wf", "run", 2)
    finally:
        w.close()
