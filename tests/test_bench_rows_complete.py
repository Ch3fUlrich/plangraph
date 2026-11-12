"""D-5173: exact effort coverage, provenance, and lossless catalog upgrades."""
from datetime import date
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import unquote, urlsplit

import pytest

from plangraph.catalog_data import SEED_MODELS
from plangraph.planner import legs
from plangraph.planner.benchmarks import benchmark_rows_from_db
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.routing_support import catalog_benchmarks
from plangraph.spawner.bench_entry import BENCHMARK_MODEL_ALIASES, LEG_BENCHMARK_MODEL

# No composite or component evaluation for these exact served variants.
UNMEASURED = {
    "agy-gpt-oss-120b", "omniroute-grok-4.7", "claude-haiku-5.5",
    "omniroute/gemini-3.8-flash",
}
SOURCES_PATH = Path(catalog_benchmarks.__file__).with_name("catalog_benchmark_sources.json")
SOURCES = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
SLUGS = {
    "agy/gemini-3.8-flash-tiered-high": "gemini-3-8-flash",
    "gemini-3.8-flash-high": "gemini-3-8-flash",
    "gemini/gemini-4-argon-high": "gemini-4-argon",
    "deepseek/deepseek-v4-flash-max": "deepseek-v4-flash",
}


def assert_provenance(row):
    assert date.fromisoformat(row["source_date"]).isoformat() == row["source_date"]
    model = row["model_id"]
    if model not in SOURCES["published"]:
        historical = SOURCES["legacy"][model]
        provenance = historical["provenance"]
        assert row["source_url"] == catalog_benchmarks.blob_url(provenance["source_oid"], provenance["source_line"])
        assert row["source_date"] == historical["provenance"]["source_date"]
        assert row["source_url"].startswith("https://api.github.com/repositories/1407069640/git/blobs/")
        assert urlsplit(row["source_url"]).fragment.startswith("L")
        for key, value in historical["metrics"].items():
            if key != "updated_at":
                assert row[key] == value, (model, key)
        return
    evidence = SOURCES["published"][model]
    blob = SOURCES_PATH.read_bytes()
    oid = hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest()
    url = urlsplit(row["source_url"])
    assert url.scheme == "https" and url.netloc == "api.github.com"
    assert url.path == "/repositories/1407069640/git/blobs/" + oid
    assert unquote(url.fragment) == "published/" + model
    assert row["source_date"] == evidence["source_date"] == SOURCES["observed_on"]
    for key, value in evidence["metrics"].items():
        assert row[key] == value, (model, key)
    slug = model.rsplit("/", 1)[-1].removeprefix("cx-").replace(".", "-")
    effort = next((e for e in legs.CX_EFFORTS if slug.endswith("-" + e)), None)
    if effort == "max" and slug.startswith("gpt-"):
        slug = slug.removesuffix("-max")
    if "muse-spark" in model:
        assert evidence["source_url"] == "https://opencode.ai/data/meta/" + slug + ".json"
        assert "Contributor" in evidence["title"] and "max effort" in evidence["excerpt"]
        assert "xhigh" not in evidence["excerpt"]
    else:
        assert evidence["source_url"] == "https://artificialanalysis.ai/models/" + SLUGS.get(model, slug)
        if effort or "tiered-high" in model:
            assert (effort or "high").lower() in evidence["title"].lower()
        assert "Model summary" in evidence["excerpt"]
        patterns = {
            "aa_intelligence": r"([\d.]+) Artificial Analysis Intelligence Index",
            "verified_tok_per_s": r"Output speed ([\d.]+)",
            "cost_per_task_usd": r"\$([\d.]+) Cost per Intelligence Index task",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, evidence["excerpt"])
            assert row[key] == (float(match[1]) if match else None), (model, key)
        if row["scicode"] is not None and "scicode" in evidence["metrics"]:
            match = re.search(r'scicode\\?":([\d.]+)', evidence["component_excerpt"])
            assert match and row["scicode"] == pytest.approx(float(match[1]) * 100)
    assert evidence["excerpt"]


def test_raw_rows_match_frozen_upstream_sources():
    path = Path(catalog_benchmarks.__file__).with_name("catalog_benchmarks.json")
    for row in json.loads(path.read_text(encoding="utf-8")):
        evidence = SOURCES["published"][row["model_id"]]
        assert row["source_url"] == evidence["source_url"]
        assert row["source_date"] == evidence["source_date"]


def test_installed_catalog_includes_snapshot_data():
    root = Path(__file__).resolve().parents[1]
    packaging = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = packaging["tool"]["setuptools"]["package-data"]["plangraph"]
    assert "config/*.json" in patterns and "**/*.sql" in patterns
    assets = list(SOURCES_PATH.parent.glob("catalog_benchmark*.json"))
    assert len(assets) == 3
    for asset in assets:
        relative = asset.relative_to(root / "src" / "plangraph").as_posix()
        assert any(fnmatchcase(relative, pattern) for pattern in patterns), relative


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
                if leg.id == "omniroute-grok-4.7":
                    # Existing audited explicit-only seat; never an automatic route.
                    from plangraph.planner.route_policy import RoutePolicy
                    policy = RoutePolicy(review_allowed_families=("grok",))
                    for role in ("review", "implement", "orchestrate"):
                        assert policy.skip_reason(leg, role) == "off_family"
                else:
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
            assert any(row.get(key) is not None for key in ("aa_intelligence", "scicode", "deepswe"))
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
def test_catalog_query_exposes_source_and_date(tmp_path, method, args):
    with CatalogDB(tmp_path / "queries.db") as db:
        db.init_and_seed()
        result = getattr(db, method)(*args)
        for row in result if isinstance(result, list) else [result]:
            if any(row[key] is not None for key in ("aa_intelligence", "scicode", "deepswe")):
                assert_provenance(row)


def test_future_active_leg_without_a_row_fails(tmp_path, monkeypatch):
    from dataclasses import replace
    leg = replace(legs.DEFAULT_LEGS[0], id="future-active", model="test/unmeasured", disabled=None)
    monkeypatch.setattr(legs, "DEFAULT_LEGS", (*legs.DEFAULT_LEGS, leg))
    with pytest.raises(AssertionError, match="future-active"):
        test_bench_rows_complete(tmp_path)


def test_exact_effort_scores_and_components(tmp_path):
    from plangraph.spawner.bench_entry import benchmark_options
    path = tmp_path / "scores.db"
    with CatalogDB(path) as db:
        db.init_and_seed()
        for effort, score in zip(legs.CX_EFFORTS, (35, 46, 50, 52, 55)):
            assert db.get_model("omniroute/cx-gpt-6.1-sol-" + effort)["aa_intelligence"] == score
        for effort, score in (("low", 54), ("medium", 56), ("high", 62)):
            row = db.get_model("claude-fable-5-2-" + effort)
            assert row["aa_intelligence"] == score
            assert row["available"] == 1  # explicit selection remains spawnable
        muse = db.get_model("opencode-go/muse-spark-1.3-contributor")
        assert muse["aa_intelligence"] is None and muse["deepswe"] == 75.4
        assert muse["scicode"] is None and muse["verified_tok_per_s"] is None
        assert muse["cost_per_task_usd"] is None
    ranked = benchmark_options(False, str(path))["benchmark"]
    assert muse["model_id"] not in ranked
    assert "gemini-3.8-flash-low" not in ranked  # published score, unknown task cost


def test_migrate_original_not_null_schema_without_data_loss(tmp_path):
    from plangraph.routing_support.catalog_benchmarks import migrate
    from plangraph.routing_support.catalog_db import SCHEMA_PATH
    with CatalogDB(tmp_path / "original.db") as db:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        for column in ("aa_intelligence", "verified_tok_per_s", "cost_per_task_usd"):
            schema = schema.replace(column + " REAL,", column + " REAL NOT NULL,")
        db.conn.executescript(schema)
        db._insert_records("catalog_providers", [{"provider_id": "test", "tier": "free", "transport": "cli", "updated_at": "2026-11-12"}])
        db._insert_records("catalog_models", [{"model_id": "test/model", "provider_id": "test", "upstream_id": "model", "family": "test", "context_tokens": 1, "max_output_tokens": 1, "status": "active"}])
        db.conn.execute("INSERT INTO catalog_benchmarks (model_id,aa_intelligence,verified_tok_per_s,cost_per_task_usd,updated_at) VALUES ('test/model',1,2,3,'unchanged')")
        before = dict(db.conn.execute("SELECT * FROM catalog_benchmarks").fetchone())
        migrate(db.conn)
        assert dict(db.conn.execute("SELECT * FROM catalog_benchmarks").fetchone()) == before
        db.conn.execute("UPDATE catalog_benchmarks SET aa_intelligence=NULL,cost_per_task_usd=NULL")


def test_query_catalog_tool_returns_provenance(tmp_path, monkeypatch):
    import asyncio

    from plangraph.spawner import mcp
    with CatalogDB(tmp_path / "mcp.db") as db:
        db.init_and_seed()
        monkeypatch.setattr(mcp, "get_catalog_models", db.list_models)
        server = mcp.MCPSpawnerServer(state=tmp_path)
        result = asyncio.run(server.call_tool("plangraph_agent_query_catalog", {}))
        assert result["count"]
        for row in result["models"]:
            assert_provenance(row)
        filtered = asyncio.run(server.call_tool("plangraph_agent_query_catalog", {"min_intelligence": 40}))
        assert all(row["aa_intelligence"] >= 40 for row in filtered["models"])


@pytest.mark.parametrize("model,change", [
    ("gemini-3.8-flash-high", {"source_url": "https://artificialanalysis.ai/models/gemini-3-8-flash"}),
    ("gemini-3.8-flash-high", {"source_date": "2026-11-11"}),
    ("omniroute/cx-gpt-6.1-sol-medium", {"model_id": "omniroute/cx-gpt-6.1-sol"}),
    ("opencode-go/muse-spark-1.3-contributor", {"scicode": 59.722222}),
    ("deepseek/deepseek-v4-flash-max", {"source_url": "https://artificialanalysis.ai/models/deepseek-v4-flash"}),
])
def test_provenance_rejects_live_mismatched_or_changed_evidence(tmp_path, model, change):
    with CatalogDB(tmp_path / "invalid.db") as db:
        db.init_and_seed()
        with pytest.raises((AssertionError, KeyError)):
            assert_provenance(dict(db.get_model(model), **change))


def test_seed_import_does_not_load_planner_policy(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", """
import sys
from plangraph.catalog_data import SEED_MODELS
from plangraph.routing_support.catalog_seed import SEED_BENCHMARKS
assert SEED_MODELS and SEED_BENCHMARKS
assert not any(m == 'plangraph.planner' or m.startswith('plangraph.planner.') for m in sys.modules)
"""], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_explicit_catalog_providers_and_reader_aliases(tmp_path):
    from plangraph.routing_support.catalog_seed import SEED_PROVIDERS
    from plangraph.spawner.bench_entry import benchmark_options
    models = {r["model_id"]: r for r in SEED_MODELS}
    providers = {r["provider_id"] for r in SEED_PROVIDERS}
    for model, row in models.items():
        assert row["provider_id"] in providers, model
    assert models["console-meta/muse-spark-1.3-contributor"]["provider_id"] == "console-meta"
    assert models["omniroute/cx-gpt-6.1-sol-low"]["provider_id"] == "cx"
    assert models["gemini-3.8-flash-high"]["provider_id"] == "agy"
    path = tmp_path / "aliases.db"
    with CatalogDB(path) as db:
        db.init_and_seed()
        assert "omniroute/cx-gpt-6.1-sol" not in SOURCES["published"]
        assert "omniroute/cx-gpt-6-luna" not in SOURCES["published"]
        assert "gemini-3.8-flash" not in SOURCES["published"]
        rows = benchmark_options(False, str(path))["benchmark"]
        for alias, canonical in BENCHMARK_MODEL_ALIASES.items():
            assert rows[alias] is rows[canonical]
            assert rows[alias]["model_id"] == canonical
            assert_provenance(rows[alias])


@pytest.mark.parametrize("model,field,value", [
    ("gemini-3.8-flash-high", "source_url", "https://artificialanalysis.ai/models/gemini-3-8-flash-medium"),
    ("deepseek/deepseek-v4-flash-max", "title", "DeepSeek V4 Flash (low)"),
    ("opencode-go/muse-spark-1.3-contributor", "title", "Muse Spark 1.3 (xhigh)"),
])
def test_provenance_rejects_wrong_upstream_identity(tmp_path, monkeypatch, model, field, value):
    with CatalogDB(tmp_path / "wrong-source.db") as db:
        db.init_and_seed()
        monkeypatch.setitem(SOURCES["published"][model], field, value)
        with pytest.raises(AssertionError):
            assert_provenance(db.get_model(model))
