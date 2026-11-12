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
        model = db.get_model("muse-amber", metric_view="benchmark")
        assert model is not None, "muse-amber"
        assert model["status"] == "active" and model["available"] == 0
        assert model["benchmark_only"] == 1 and model["unavailable_reason"]
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
        db.conn.execute("DELETE FROM catalog_model_seed_versions WHERE model_id=?", (muse["model_id"],))
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
        assert_provenance(db.get_model(previous["model_id"], metric_view="benchmark"))
        legacy = SOURCES["legacy"]["mimo/mimo-v2.6-pro"]["metrics"]
        db.conn.execute("DELETE FROM catalog_benchmarks WHERE model_id=?", (legacy["model_id"],))
        db._insert_records("catalog_benchmarks", [dict(legacy, swe_atlas_qna=12.345)])
        db.init_and_seed()
        assert db.get_model(legacy["model_id"])["swe_atlas_qna"] == 12.345


def test_nullable_observations_do_not_replace_calibrated_routing_inputs(tmp_path):
    from plangraph.routing_support.resolver import resolve_model_for_task
    with CatalogDB(tmp_path / "routing.db") as db:
        db.init_and_seed()
        for model, evidence in SOURCES["legacy"].items():
            routed = db.get_model(model, metric_view="routing")
            assert routed["metric_view"] == "routing"
            assert routed["aa_intelligence"] == evidence["metrics"]["aa_intelligence"]
            assert routed["benchmark"]["aa_intelligence"] == db.get_model(model, metric_view="benchmark")["aa_intelligence"]
        assert resolve_model_for_task(db=db)["model"] == "mimo/mimo-v2.6-pro"


def test_model_seed_corrections_reach_existing_db_without_operator_loss(tmp_path):
    with CatalogDB(tmp_path / "identity.db") as db:
        db.init_and_seed()
        model = "muse-amber"
        db.conn.execute("UPDATE catalog_models SET available=1,context_tokens=0 WHERE model_id=?", (model,))
        db.conn.execute("DELETE FROM catalog_model_seed_versions WHERE model_id=?", (model,))
        db.conn.execute("UPDATE catalog_models SET context_tokens=123456 WHERE model_id='mimo/mimo-v2.6-pro'")
        db.init_and_seed()
        row = db.get_model(model)
        assert row["available"] == 0 and row["unavailable_reason"]
        assert db.get_model("mimo/mimo-v2.6-pro")["context_tokens"] == 123456


def test_three_way_seed_refresh_preserves_operator_columns(tmp_path):
    with CatalogDB(tmp_path / "three-way.db") as db:
        db.init_and_seed()
        model = db.get_model("mimo/mimo-v2.6-pro")
        db.conn.execute("UPDATE catalog_models SET context_tokens=123456 WHERE model_id=?", (model["model_id"],))
        seed = next(r for r in catalog_seed.SEED_MODELS if r["model_id"] == model["model_id"])
        db._insert_records("catalog_models", [dict(seed, context_tokens=2000000, max_output_tokens=77777)], seed=True)
        updated = db.get_model(model["model_id"])
        assert updated["context_tokens"] == 123456 and updated["max_output_tokens"] == 77777


def test_all_catalog_identities_pass_registry_tagging(monkeypatch):
    from plangraph import catalog_data
    tagged = []
    original = catalog_data.tag_seed_rows
    def record(rows):
        tagged.extend(row["model_id"] for row in rows)
        return original(rows)
    monkeypatch.setattr(catalog_data, "tag_seed_rows", record)
    base = [dict(row) for row in catalog_data.SEED_MODELS if not row.get("benchmark_only")]
    rows = catalog_data.model_rows(base)
    assert set(tagged) == {row["model_id"] for row in rows}


def test_benchmark_only_rows_are_visible_but_never_admitted(tmp_path, monkeypatch):
    from plangraph.routing_support import resolver
    from plangraph.spawner import mcp
    with CatalogDB(tmp_path / "catalog-only.db") as db:
        db.init_and_seed()
        monkeypatch.setattr(resolver, "_get_default_catalog_db", lambda: db)
        result = asyncio.run(mcp.MCPSpawnerServer(state=tmp_path).call_tool("plangraph_agent_query_catalog", {}))
        rows = [row for row in result["models"] if row.get("benchmark_only")]
        assert rows
        for row in rows:
            assert row["available"] == 0 and row["active"] is False
            assert row["unavailable_reason"] and not resolver.is_model_available(row)


def assert_live_source(model, content):
    """Check publisher bytes, not authored excerpts or mere HTTP success."""
    import re
    evidence = SOURCES["published"][model]
    if "muse-spark" in model:
        import hashlib
        assert hashlib.sha256(content).hexdigest() == SOURCES["publisher_assets"]["muse_scorecard_sha256"]
        return
    text = content.decode("utf-8")
    metrics = evidence["metrics"]
    if model == "opencode/big-pickle":
        assert '"task_resolve_rate": 0.5081' in text and '"resolved": 63' in text
        assert '"model": "big-pickle (OpenCode Zen)"' in text
    elif model == "claude-haiku-5-5":
        assert "claude-haiku-5-5" in text and "39.2" in text and "Terminal-Bench" in text
    elif model == "gpt-oss-120b-medium":
        assert "gpt-oss-120b" in text and "52.6" in text and "SWE" in text
    else:
        from html import unescape
        text = unescape(text)
        assert evidence["title"].split(" - Intelligence")[0].lower() in text.lower()
        visible = re.sub(r"<[^>]+>", " ", text)
        visible = re.sub(r"\s+", " ", visible)
        for key, label in (("aa_intelligence", "Artificial Analysis Intelligence Index"),
                           ("verified_tok_per_s", "tok/s"), ("cost_per_task_usd", "Cost per Intelligence Index task")):
            value = metrics.get(key)
            if value is not None:
                number = f"{value:g}" if key != "cost_per_task_usd" else f"{value:.2f}"
                assert re.search(re.escape(number) + r"\s*" + re.escape(label), visible), (model, key)
        if metrics.get("scicode") is not None:
            match = re.search(r'scicode\\?":([\d.]+)', text)
            assert match and float(match[1]) * 100 == pytest.approx(metrics["scicode"]), model


def test_live_publisher_verification_rejects_unrelated_success_page():
    with pytest.raises(AssertionError):
        assert_live_source("mimo/mimo-v2.6-pro", b"<html>Generic model listing</html>")


def test_live_publisher_sources():
    """Explicit network audit; ordinary catalog tests remain hermetic."""
    import os
    import urllib.request
    if os.environ.get("PLANGRAPH_VERIFY_BENCHMARK_SOURCES") != "1":
        pytest.skip("opt-in publisher source audit")
    cache = {}
    for model, evidence in SOURCES["published"].items():
        url = evidence["source_url"]
        if url is None:
            assert model in UNVERIFIABLE  # Unresolved, never a passing measurement.
            continue
        if model == "opencode/big-pickle":
            url = url.replace("github.com/", "raw.githubusercontent.com/").replace("/blob/", "/").split("#")[0]
        if url not in cache:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as response:
                cache[url] = response.read()
        assert_live_source(model, cache[url])


def test_component_only_seed_keeps_calibrated_utility_without_false_measurement(tmp_path):
    from plangraph.routing_support.resolver import score_model
    path = tmp_path / "utility.db"
    with CatalogDB(path) as db:
        db.init_and_seed()
        raw = db.get_model("opencode-go/deepseek-v4.1-flash", metric_view="benchmark")
        assert raw["aa_intelligence"] is None and raw["cost_per_task_usd"] is None
    routed = benchmark_options(False, str(path))["benchmark"][raw["model_id"]]
    assert routed["metric_view"] == "routing" and routed["benchmark"]["aa_intelligence"] is None
    assert isinstance(score_model(routed), float)


def test_null_operator_benchmark_does_not_abort_routing(tmp_path):
    from plangraph.routing_support.resolver import resolve_model_for_task, resolve_review_plan
    with CatalogDB(tmp_path / "null-operator.db") as db:
        db.init_and_seed()
        db.conn.execute("UPDATE catalog_benchmarks SET aa_intelligence=NULL WHERE model_id='opencode-go/deepseek-v4.1-flash'")
        assert resolve_model_for_task(db=db)["model"] == "mimo/mimo-v2.6-pro"
        assert resolve_review_plan("agy/claude-sonnet-5-5-high", risk="high", db=db)["reviewer_count"] == 2


def test_argon_sonnet_high_risk_throughput_tie_is_still_exercised(tmp_path):
    from plangraph.routing_support.resolver import rank_candidates, score_model
    with CatalogDB(tmp_path / "tie.db") as db:
        db.init_and_seed()
        argon = db.get_model("opencode-go/gemini-4-argon")
        sonnet = db.get_model("agy/claude-sonnet-5-5-high")
        sonnet = dict(sonnet, aa_intelligence=argon["aa_intelligence"], cost_per_task_usd=argon["cost_per_task_usd"] + 0.05,
                      verified_tok_per_s=argon["verified_tok_per_s"] + 1)
        assert 0 < score_model(argon, risk="high") - score_model(sonnet, risk="high") <= 0.3
        assert rank_candidates([argon, sonnet], risk="high")[0]["id"] == sonnet["id"]
