"""Fail-closed identity, source and alias regressions for BENCH-DB-UPDATE."""
import asyncio

import pytest

from plangraph.routing_support import catalog_seed
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.spawner.bench_entry import benchmark_options
from test_bench_rows_complete import SOURCES, UNVERIFIABLE, assert_provenance


def test_item_required_identity_cannot_be_omitted_or_inactive(tmp_path):
    with CatalogDB(tmp_path / "required.db") as db:
        db.init_and_seed()
        model = db.get_model("muse-amber")
        assert model is not None, "muse-amber"
        assert model["status"] == "active" and model["available"] == 1
        assert model["source_date"] == SOURCES["observed_on"]
        # Unresolved acceptance requirement: never invent an Ember/Spark alias.
        assert model["source_url"] is None and model["benchmark_source"].startswith("UNMEASURED:")


def test_muse_contributor_identity_does_not_claim_a_different_effort(tmp_path):
    from plangraph.catalog_data import SEED_MODELS
    muse = next(row for row in SEED_MODELS if row["model_id"] == "opencode-go/muse-spark-1.3-contributor")
    assert muse["upstream_id"] == "muse-spark-1.3-contributor"
    assert muse["parameter_effort"] == "max"
    with CatalogDB(tmp_path / "muse-upgrade.db") as db:
        db.init_and_seed()
        db.conn.execute("UPDATE catalog_models SET upstream_id='muse-spark-1.3',parameter_effort='xhigh' WHERE model_id=?",
                        (muse["model_id"],))
        db.init_and_seed()
        assert db.get_model(muse["model_id"])["parameter_effort"] == "max"


def test_all_unmeasured_sources_are_not_misrepresented_as_measurements():
    for row in catalog_seed.published_rows():
        if row["benchmark_source"].startswith("UNMEASURED:"):
            assert row["source_url"] is None, row["model_id"]


def test_seed_citations_are_upstream_and_effort_scores_agree(tmp_path):
    with CatalogDB(tmp_path / "upstream.db") as db:
        db.init_and_seed()
        by_source = {}
        for row in db.conn.execute("SELECT * FROM catalog_benchmarks"):
            assert_provenance(row)
            if row["source_url"] is None:
                assert row["model_id"] in UNVERIFIABLE
                continue
            metrics = tuple(row[key] for key in ("aa_intelligence", "verified_tok_per_s", "cost_per_task_usd"))
            previous = by_source.setdefault(row["source_url"], metrics)
            assert metrics == previous, row["model_id"]


@pytest.mark.parametrize("first", [True, False])
def test_measured_alias_survives_all_database_orders(tmp_path, first):
    import sqlite3
    paths = [tmp_path / "measured.db", tmp_path / "canonical.db"]
    models = ["omniroute/cx-gpt-6-luna", "omniroute/cx-gpt-6-luna-medium"]
    for path, model, score in zip(paths, models, (38.4, 18)):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE catalog_benchmarks (model_id TEXT, aa_intelligence REAL)")
            conn.execute("INSERT INTO catalog_benchmarks VALUES (?, ?)", (model, score))
    rows = benchmark_options(False, [str(p) for p in paths[::1 if first else -1]])["benchmark"]
    assert rows[models[0]]["model_id"] == models[0]
    assert rows[models[0]]["aa_intelligence"] == 38.4
    assert rows[models[1]]["aa_intelligence"] == 18


def test_query_catalog_regression_detects_removed_benchmark_only_flag(tmp_path, monkeypatch):
    from plangraph.routing_support import resolver
    from plangraph.spawner import mcp
    with CatalogDB(tmp_path / "flag.db") as db:
        db.init_and_seed()
        monkeypatch.setattr(resolver, "_get_default_catalog_db", lambda: db)
        server = mcp.MCPSpawnerServer(state=tmp_path)
        result = asyncio.run(server.call_tool("plangraph_agent_query_catalog", {}))
        ids = {row["model_id"] for row in result["models"]}
        assert "muse-amber" in ids and "omniroute/cx-gpt-6.1-sol-xhigh" in ids
        # Exercise the real reader in both modes, not a permissive **kw stub.
        default_ids = {row["model_id"] for row in resolver.get_catalog_models()}
        assert "muse-amber" not in default_ids and "omniroute/cx-gpt-6.1-sol-xhigh" not in default_ids
        real_reader = mcp.get_catalog_models
        monkeypatch.setattr(mcp, "get_catalog_models", lambda **kwargs: real_reader())
        without_flag = asyncio.run(server.call_tool("plangraph_agent_query_catalog", {}))
        assert "omniroute/cx-gpt-6.1-sol-xhigh" not in {row["model_id"] for row in without_flag["models"]}


def test_seed_refresh_preserves_operator_values_with_old_citation(tmp_path):
    with CatalogDB(tmp_path / "operator.db") as db:
        db.init_and_seed()
        historical = SOURCES["legacy"]["mimo/mimo-v2.6-pro"]["metrics"]
        db.conn.execute("DELETE FROM catalog_benchmarks WHERE model_id=?", (historical["model_id"],))
        db._insert_records("catalog_benchmarks", [dict(historical, aa_intelligence=12.345,
                           source_url="https://api.github.com/repositories/1407069640/git/blobs/old#L1",
                           source_date="2026-10-19")])
        before = dict(db.conn.execute("SELECT * FROM catalog_benchmarks WHERE model_id=?",
                                     (historical["model_id"],)).fetchone())
        db.init_and_seed()
        after = dict(db.conn.execute("SELECT * FROM catalog_benchmarks WHERE model_id=?",
                                    (historical["model_id"],)).fetchone())
        assert after == before


def test_prior_pr_seed_is_refreshed_without_relabelling_operator_components(tmp_path):
    with CatalogDB(tmp_path / "prior-pr.db") as db:
        db.init_and_seed()
        previous = dict(model_id="claude-opus-5-5-low", aa_intelligence=35,
                        verified_tok_per_s=107, cost_per_task_usd=0.98,
                        source_url="https://artificialanalysis.ai/models/claude-opus-5-5-low",
                        source_date="2026-11-12", benchmark_source="AA-v5.0.1 snapshot",
                        updated_at="2026-11-12T00:00:00Z")
        db.conn.execute("DELETE FROM catalog_benchmarks WHERE model_id=?", (previous["model_id"],))
        db._insert_records("catalog_benchmarks", [previous])
        db.init_and_seed()
        assert_provenance(db.get_model(previous["model_id"]))
        legacy = SOURCES["legacy"]["mimo/mimo-v2.6-pro"]["metrics"]
        db.conn.execute("DELETE FROM catalog_benchmarks WHERE model_id=?", (legacy["model_id"],))
        db._insert_records("catalog_benchmarks", [dict(legacy, swe_atlas_qna=12.345)])
        db.init_and_seed()
        assert db.get_model(legacy["model_id"])["swe_atlas_qna"] == 12.345
