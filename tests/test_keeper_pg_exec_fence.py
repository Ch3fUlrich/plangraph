"""Behavioural Postgres coverage for the exec-owner fence (RT-P1 V3-A).

``test_keeper_pg_unit.py`` pins migration 7's DDL structurally and only SQLite
ran the real behaviour; this file applies migration 7 on a real Postgres
schema and then exercises owner-fence calls against it: a park carrying an
older attempt than the recorded owner must refuse ``exec_stale`` and write
nothing, and an alarm carrying a newer attempt must advance the recorded
attempt while an open alarm row already exists (the FIX2 case), exactly as on
SQLite.  Needs a Postgres server: set ``PG_DSN`` (CI provides one); skipped
otherwise.
"""
import os

import pytest

from keeper_util import World, seed, seed_exec_owner, to_claimed
from plangraph.keeper.errors import Refused

DSN = os.environ.get("PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="PG_DSN not set (Postgres service container)")

#: The exec identity the router records first (attempt 2).
RUN2 = {"workflow_id": "wf", "run_id": "run", "attempt": 2}


@needs_pg
@pytest.mark.parametrize("verb", ["complete", "alarm"])
@pytest.mark.parametrize("owned", [False, True])
def test_expired_production_claim_pg(tmp_path, verb, owned):
    from test_keeper_exec_sq10 import check_expired_claim

    w = World(tmp_path, backend="postgres", dsn=DSN)
    try:
        check_expired_claim(w, verb, owned)
    finally:
        w.close()


def owner(w, key):
    row = w.conn.execute(
        "SELECT exec_workflow_id, exec_run_id, exec_attempt FROM items WHERE key=?",
        (key,)).fetchone()
    return tuple(row)


@needs_pg
@pytest.mark.parametrize("who", ["workerA", "workerB"])
def test_duplicate_complete_pg(tmp_path, who):
    from test_keeper_exec_sq11 import check_duplicate_complete

    w = World(tmp_path, backend="postgres", dsn=DSN)
    try:
        check_duplicate_complete(w, who)
    finally:
        w.close()


@needs_pg
def test_migration_7_applies_on_pg_and_park_refuses_an_older_attempt(tmp_path):
    d = tmp_path / "pgexec"
    d.mkdir()
    w = World(d, backend="postgres", dsn=DSN)
    try:
        # Migration 7 (the three exec ownership columns) was applied here.
        versions = [row[0] for row in w.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        assert 7 in versions, versions
        key, = seed(w, [("A", [])])
        # A run-carrying move records the owner at attempt 2 on Postgres ...
        seed_exec_owner(w, key, ("wf", "run", 2))
        w.store.set_state(w.p("router"), key, "running")
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


@needs_pg
def test_open_alarm_on_an_owned_card_advances_the_attempt(tmp_path):
    """The FIX2 alarm case on Postgres: an open alarm row does not freeze the fence."""
    d = tmp_path / "pgexecalarm"
    d.mkdir()
    w = World(d, backend="postgres", dsn=DSN)
    try:
        key, = seed(w, [("A", [])])
        alarm = {"card": key, "reason": "stuck"}
        to_claimed(w, key, "workerA")
        # A first alarm opens the fact and records run R at attempt 1.
        w.store.worker_alarm(w.p("workerA"),
                             {**alarm, "workflow_id": "wf", "run_id": "run",
                              "attempt": 1})
        assert owner(w, key) == ("wf", "run", 1)
        # The same run at attempt 2 while the alarm is still open: the row is a
        # duplicate fact, the recorded attempt still advances to 2 ...
        w.store.worker_alarm(w.p("workerA"),
                             {**alarm, "workflow_id": "wf", "run_id": "run",
                              "attempt": 2})
        assert owner(w, key) == ("wf", "run", 2)
        rows = w.conn.execute(
            "SELECT COUNT(*) FROM projector_alarms WHERE kind='worker_alarm'"
            " AND key=? AND cleared_at IS NULL", (key,)).fetchone()[0]
        assert rows == 1
        # ... so the older attempt of the same run is refused, as on SQLite.
        with pytest.raises(Refused) as exc:
            w.store.worker_park(w.p("workerA"), {
                "card": key, "tier": "free", "exit_class": "check_fail",
                "workflow_id": "wf", "run_id": "run", "attempt": 1})
        assert exc.value.code == "exec_stale"
        assert owner(w, key) == ("wf", "run", 2)
    finally:
        w.close()
