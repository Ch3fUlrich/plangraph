"""D-5173: exact effort coverage, provenance, and lossless catalog upgrades."""
from datetime import date

import pytest

from plangraph.catalog_data import SEED_MODELS
from plangraph.planner import legs
from plangraph.planner.benchmarks import benchmark_rows_from_db
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.spawner.bench_entry import LEG_BENCHMARK_MODEL


# No composite or component evaluation for these exact served variants.
UNMEASURED = {
    "agy-gpt-oss-120b", "omniroute-grok-4.7", "claude-haiku-5.5",
    "claude/claude-haiku-4-5", "omniroute/gemini-3.8-flash",
}


def assert_provenance(row):
    assert row["source_url"].startswith("https://")
    assert date.fromisoformat(row["source_date"]).isoformat() == row["source_date"]


def test_bench_rows_complete(tmp_path):
    path = tmp_path / "catalog.db"
    with CatalogDB(path) as db:
        db.init_and_seed()
        rows = {r["model_id"]: dict(r) for r in db.conn.execute("SELECT * FROM catalog_benchmarks")}
        models = {r["model_id"]: r for r in db.list_models()}
        explicit = {leg.id for leg in legs.EXPLICIT_LEGS}
        seen = set()
        for leg in legs.spawnable_legs():
            if leg.id in UNMEASURED:
                seen.add(leg.id)
                assert leg.disabled or leg.id in explicit, leg.id
                assert leg.model not in rows, leg.id
                continue
            if leg.disabled:
                continue
            model = LEG_BENCHMARK_MODEL.get(leg.id, leg.model)
            assert model in rows, (leg.id, model)
            assert_provenance(rows[model])
        for seed in SEED_MODELS:
            model = seed["model_id"]
            if model in UNMEASURED:
                seen.add(model)
                assert not models[model]["available"], model
                assert model not in rows
            elif seed["status"] == "active" or seed["available"]:
                assert model in rows, model
                assert_provenance(rows[model])
        assert seen == UNMEASURED
        for row in rows.values():
            assert_provenance(row)
    loaded, errors = benchmark_rows_from_db(path)
    assert not errors
    for row in loaded.values():
        assert_provenance(row)


def test_provenance_migration_preserves_operator_rows(tmp_path):
    path = tmp_path / "legacy.db"
    with CatalogDB(path) as db:
        db.init_and_seed()
        db.conn.execute("UPDATE catalog_benchmarks SET aa_intelligence=12.345 WHERE model_id='mimo/mimo-v2.6-pro'")
        db.conn.execute("ALTER TABLE catalog_benchmarks DROP COLUMN source_url")
        db.conn.execute("ALTER TABLE catalog_benchmarks DROP COLUMN source_date")
        db.conn.commit()
        db.init_and_seed()
        row = db.get_model("mimo/mimo-v2.6-pro")
        assert row["aa_intelligence"] == 12.345
        # A legacy operator score must not acquire the seed's citation.
        assert row["source_url"] is None and row["source_date"] is None
        db.init_and_seed()
        assert db.get_model("mimo/mimo-v2.6-pro") == row


@pytest.mark.parametrize("method,args", [("list_models", ()), ("get_model", ("mimo/mimo-v2.6-pro",)),
                                         ("get_route_legs", ("l2-worker",))])
def test_catalog_query_exposes_source_and_date(method, args):
    with CatalogDB() as db:
        db.init_and_seed()
        result = getattr(db, method)(*args)
        for row in result if isinstance(result, list) else [result]:
            if row["aa_intelligence"] is not None:
                assert_provenance(row)


def test_future_active_leg_without_a_row_fails(tmp_path, monkeypatch):
    from dataclasses import replace
    leg = replace(legs.DEFAULT_LEGS[0], id="future-active", model="test/unmeasured", disabled=None)
    monkeypatch.setattr(legs, "DEFAULT_LEGS", (*legs.DEFAULT_LEGS, leg))
    with pytest.raises(AssertionError, match="future-active"):
        test_bench_rows_complete(tmp_path)
