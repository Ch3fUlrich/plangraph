"""HOST-SAMPLES-API: keeper-owned maintenance, retention and runtime privileges."""
import inspect

import pytest

from test_keeper_host_samples import world, sample, host_principal, DSN
from plangraph.keeper import daemon, schema_pg

pytestmark = pytest.mark.skipif(not DSN, reason="PLANGRAPH_TEST_PG_DSN unset: needs Postgres 16")


def test_pg_partitioned_raw_and_migration(world):
    from plangraph.keeper import store_host_samples
    assert set(store_host_samples.TABLES) == {
        "host_samples_raw", "host_samples_1m", "host_samples_1h", "host_sample_tokens"}
    assert schema_pg.LATEST == 12
    row = world.conn.execute(
        "SELECT partstrat FROM pg_partitioned_table WHERE partrelid="
        "'host_samples_raw'::regclass").fetchone()
    assert row[0] == "r"


def test_pg_rollup_idempotence_and_range(world):
    who, _ = host_principal(world)
    start = world.clock()
    world.store.ingest_host_sample(who, sample())
    world.clock.advance(10)
    body = sample(2)
    body["cpu_pct"] = {"mean": 40, "max": 50}
    world.store.ingest_host_sample(who, body)
    world.clock.advance(7200)
    world.store.maintain_host_samples()
    first = {}
    for resolution in ("1m", "1h"):
        rows = world.store.host_samples_range(world.p("reader"), "host-a",
                                              start - 3600, world.clock(), resolution)
        assert len(rows) == 1
        assert rows[0]["sample"]["cpu_pct"] == {"mean": 30, "max": 50}
        assert rows[0]["sample_count"] == 2
        first[resolution] = rows
    world.store.maintain_host_samples()
    for resolution, rows in first.items():
        assert world.store.host_samples_range(world.p("reader"), "host-a",
                                              start - 3600, world.clock(), resolution) == rows


def test_pg_retention_all_tiers(world):
    who, _ = host_principal(world)
    world.store.ingest_host_sample(who, sample())
    world.clock.advance(15 * 86400)
    world.store.maintain_host_samples()
    assert world.conn.execute("SELECT count(*) FROM host_samples_raw").fetchone()[0] == 0
    assert world.conn.execute("SELECT count(*) FROM host_samples_1m").fetchone()[0] == 1
    world.clock.advance(76 * 86400)
    world.store.maintain_host_samples()
    assert world.conn.execute("SELECT count(*) FROM host_samples_1m").fetchone()[0] == 0
    assert world.conn.execute("SELECT count(*) FROM host_samples_1h").fetchone()[0] == 1
    world.clock.advance(731 * 86400)
    world.store.maintain_host_samples()
    assert world.conn.execute("SELECT count(*) FROM host_samples_1h").fetchone()[0] == 0


def test_pg_runtime_cannot_ddl_but_maintenance_creates_partitions(world):
    from plangraph.keeper.db_pg import DdlDenied
    with pytest.raises(DdlDenied):
        world.conn.execute("CREATE TABLE fixture_ddl (n integer)")
    # Exercise SQL privileges too, not just the Python guard.
    raw = world.conn._raw
    raw.execute("DO $$ BEGIN CREATE ROLE keeper_rt; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    schema = world.conn.schema
    raw.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO keeper_rt')
    raw.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO keeper_rt')
    raw.execute("SET ROLE keeper_rt")
    try:
        import psycopg
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw.execute("CREATE TABLE fixture_ddl (n integer)")
        world.store.maintain_host_samples()
        world.clock.advance(86400)
        world.store.maintain_host_samples()
    finally:
        raw.execute("RESET ROLE")


def test_keeper_daemon_owns_maintenance_loop():
    source = inspect.getsource(daemon)
    assert "_host_samples_loop" in source
    assert "maintain_host_samples" in source
