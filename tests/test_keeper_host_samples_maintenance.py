"""HOST-SAMPLES-API: keeper-owned maintenance, retention and runtime privileges."""
import inspect

import pytest

from test_keeper_host_samples import world, sample, host_principal, DSN, SKIP_WITHOUT_PG
from plangraph.keeper import daemon, schema_pg

pytestmark = SKIP_WITHOUT_PG


def test_pg_partitioned_raw_and_migration(world):
    from plangraph.keeper import store_host_samples
    assert set(store_host_samples.TABLES) == {
        "host_samples_raw", "host_samples_1m", "host_samples_1h", "host_sample_tokens"}
    assert schema_pg.LATEST == 21
    migration = next(step for step in schema_pg.MIGRATIONS if step.version == 12)
    assert set(store_host_samples.TABLES) <= set(schema_pg.parse_tables(migration.sql))
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


def test_pg_rollup_saturates_summed_sequence_gaps(world):
    """Each sample is valid, but a bucket's summed gaps must still fit BIGINT."""
    who, _ = host_principal(world)
    top = 2 ** 63 - 1
    for seq, started in [(1, 1), (top, 1), (1, 2), (top, 2)]:
        world.store.ingest_host_sample(who, sample(seq, started))
        world.clock.advance(1)
    # An old raw row sits past retention: the shared maintenance step must still delete it.
    world.clock.advance(15 * 86400)
    world.store.maintain_host_samples()
    world.store.maintain_host_samples()
    row = world.conn.execute("SELECT seq_gap FROM host_samples_1m").fetchone()
    assert row[0] == top
    assert world.conn.execute("SELECT count(*) FROM host_samples_raw").fetchone()[0] == 0
    world.clock.advance(80 * 86400)
    world.store.maintain_host_samples()
    assert world.conn.execute("SELECT seq_gap FROM host_samples_1h").fetchone()[0] == top


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
    # Deployment grants the partition function to the runtime role by name (PUBLIC has none).
    raw.execute(f'GRANT EXECUTE ON FUNCTION "{schema}".host_samples_partitions(double precision) TO keeper_rt')
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


def test_pg_partition_function_is_not_executable_by_public(world):
    import psycopg
    raw = world.conn._raw
    raw.execute("DO $$ BEGIN CREATE ROLE keeper_unrelated; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    schema = world.conn.schema
    raw.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO keeper_unrelated')
    function = f'"{schema}".host_samples_partitions'
    signature = f'{function}(double precision)'
    assert not raw.execute("SELECT has_function_privilege('keeper_unrelated', %s, 'EXECUTE')",
                           (signature,)).fetchone()[0]
    raw.execute("SET ROLE keeper_unrelated")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw.execute(f"SELECT {function}(%s)", (0.0,))
    finally:
        raw.execute("RESET ROLE")


def test_pg_migration_grants_the_runtime_role_the_partition_function(world, tmp_path):
    """With ``keeper_rt`` present at upgrade time, migration 14 itself installs its EXECUTE."""
    from keeper_util import World
    world.conn._raw.execute(
        "DO $$ BEGIN CREATE ROLE keeper_rt; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    upgraded = World(tmp_path / "upgraded", backend="postgres", dsn=DSN)
    try:
        raw, schema = upgraded.conn._raw, upgraded.conn.schema
        raw.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO keeper_rt')
        raw.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO keeper_rt')
        raw.execute("SET ROLE keeper_rt")  # no explicit EXECUTE grant here
        try:
            upgraded.store.maintain_host_samples()
            upgraded.clock.advance(86400)
            upgraded.store.maintain_host_samples()
        finally:
            raw.execute("RESET ROLE")
    finally:
        upgraded.close()


def test_keeper_daemon_owns_maintenance_loop():
    source = inspect.getsource(daemon)
    assert "_host_samples_loop" in source
    assert "maintain_host_samples" in source
