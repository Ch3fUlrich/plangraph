"""Whole-asset provenance and capability contracts for BENCH-DB-UPDATE."""
from datetime import date
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import urlsplit

import pytest

from plangraph.catalog_data import SEED_MODELS
from plangraph.routing_support import catalog_seed
from plangraph.routing_support.catalog_db import CatalogDB

ASSETS = Path(catalog_seed.__file__).parent
SOURCES = json.loads((ASSETS / "catalog_benchmark_sources.json").read_text())
MODELS = json.loads((ASSETS / "catalog_benchmark_models.json").read_text())
ROWS = catalog_seed.published_rows()
METRICS = {key for row in ROWS for key in row} - {
    "model_id", "master_rank", "source_url", "source_date", "benchmark_source", "updated_at"}


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["model_id"])
def test_every_source_is_specific_or_explicitly_unmeasured(row):
    evidence = SOURCES["published"][row["model_id"]]
    assert date.fromisoformat(row["source_date"])
    assert row["source_url"] == evidence["source_url"]
    assert evidence["checked_on"] == SOURCES["observed_on"]
    assert evidence["source_note"]
    for metric in METRICS:
        assert row.get(metric) == evidence["metrics"].get(metric), (row["model_id"], metric)
    if row["source_url"] is None:
        assert row["benchmark_source"] == evidence["excerpt"]
        assert row["benchmark_source"].startswith("UNMEASURED:")
        assert SOURCES["observed_on"] in evidence["source_note"]
        assert not any(row.get(metric) is not None for metric in METRICS)
        return
    assert re.fullmatch(r"[a-f0-9]{64}", evidence["source_sha256"])
    url = urlsplit(row["source_url"])
    assert url.scheme == "https" and url.hostname
    assert url.path not in {"", "/", "/models", "/models/"}
    if url.hostname == "artificialanalysis.ai":
        slug = url.path.removeprefix("/models/")
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
        component = json.loads(evidence["component_excerpt"])
        assert component["slug"] == slug
        assert component["name"].lower() in evidence["title"].lower()
        assert row["scicode"] == pytest.approx(component["scicode"] * 100)
        model = next(model for model in SEED_MODELS if model["model_id"] == row["model_id"])
        if model["parameter_effort"]:
            assert component["effort"]["slug"] == model["parameter_effort"]
        assert "frozen" in evidence["source_note"].lower()


def test_repeated_components_have_separate_identity_bound_excerpts():
    by_value = {}
    for row in ROWS:
        if row.get("scicode") is not None:
            by_value.setdefault(row["scicode"], []).append(row)
    for group in by_value.values():
        for row in group:
            evidence = SOURCES["published"][row["model_id"]]
            component = json.loads(evidence["component_excerpt"])
            assert component["slug"] == urlsplit(row["source_url"]).path.split("/")[-1]
            assert component["scicode"] * 100 == pytest.approx(row["scicode"])


def test_all_capability_limits_and_unknown_conventions_are_consistent():
    by_upstream = {}
    for model in SEED_MODELS:
        upstream = model["upstream_id"]
        limits = (model["context_tokens"], model["max_output_tokens"])
        assert limits == by_upstream.setdefault(upstream, limits), model["model_id"]
        capability = SOURCES["capabilities"][upstream]
        assert limits == (capability["context_tokens"], capability["max_output_tokens"])
        assert capability["source_note"] and capability["checked_on"] == SOURCES["observed_on"]
        if 0 in limits:
            assert "unknown" in capability["source_note"].lower()
        if model["parameter_effort"]:
            assert not upstream.endswith("-" + model["parameter_effort"]), model["model_id"]
    for model in MODELS:
        assert model["available"] == 0 and model["unavailable_reason"]
        seeded = next(row for row in SEED_MODELS if row["model_id"] == model["model_id"])
        for key in model:
            assert seeded[key] == model[key], (model["model_id"], key)


@pytest.mark.parametrize("table,change", [
    ("catalog_models", {"status": "actve"}),
    ("catalog_models", {"context_tokens": None}),
    ("catalog_benchmarks", {"updated_at": None}),
])
@pytest.mark.parametrize("existing", [True, False])
def test_seed_conflicts_never_silence_invalid_cells(tmp_path, table, change, existing):
    with CatalogDB(tmp_path / "invalid.db") as db:
        db.init_and_seed()
        row = dict((catalog_seed.SEED_MODELS if table == "catalog_models" else ROWS)[0], **change)
        if not existing:
            row["model_id"] = "test/invalid"
        with pytest.raises(sqlite3.IntegrityError):
            db._insert_records(table, [row], seed=True)


def test_seed_duplicate_primary_key_preserves_operator_measurement(tmp_path):
    with CatalogDB(tmp_path / "duplicate.db") as db:
        db.init_and_seed()
        row = ROWS[0]
        db.conn.execute("UPDATE catalog_benchmarks SET aa_intelligence=12.345 WHERE model_id=?", (row["model_id"],))
        db._insert_records("catalog_benchmarks", [row], seed=True)
        assert db.get_model(row["model_id"], metric_view="benchmark")["aa_intelligence"] == 12.345


def test_historical_fingerprints_are_not_additional_published_observations():
    for section in ("legacy", "prior_models"):
        assert all("source_url" not in row for row in SOURCES[section].values())
    assert set(SOURCES["legacy"]) <= set(SOURCES["published"])


@pytest.mark.parametrize("model,key,value", [
    ("gemini-3.8-flash-low", "source_url", "https://artificialanalysis.ai/models/gemini-3.8-flash-low"),
    ("muse-amber", "source_note", ""),
    ("muse-amber", "checked_on", "2026-11-11"),
    ("muse-amber", "metrics", {"aa_intelligence": 1}),
])
def test_whole_row_audit_rejects_bad_slugs_missing_reasons_and_unmeasured_scores(monkeypatch, model, key, value):
    row = next(row for row in ROWS if row["model_id"] == model)
    monkeypatch.setitem(SOURCES["published"][model], key, value)
    with pytest.raises(AssertionError):
        test_every_source_is_specific_or_explicitly_unmeasured(row)


@pytest.mark.parametrize("field,value", [("slug", "mimo-v2-6-pro"), ("effort", {"slug": "high"}),
                                         ("scicode", 0.608796296296296)])
def test_component_audit_rejects_cross_model_or_effort_copy(monkeypatch, field, value):
    row = next(row for row in ROWS if row["model_id"] == "omniroute/cx-gpt-6.1-sol-max")
    evidence = SOURCES["published"][row["model_id"]]
    component = dict(json.loads(evidence["component_excerpt"]), **{field: value})
    monkeypatch.setitem(evidence, "component_excerpt", json.dumps(component))
    with pytest.raises(AssertionError):
        test_every_source_is_specific_or_explicitly_unmeasured(row)


@pytest.mark.parametrize("key,value", [("context_tokens", 900000), ("max_output_tokens", 64000),
                                      ("available", 1), ("upstream_id", "gemini-3.8-flash-high")])
def test_capability_audit_rejects_placeholders_and_hidden_admission(monkeypatch, key, value):
    row = next(row for row in SEED_MODELS if row["model_id"] == "gemini-3.8-flash-high")
    monkeypatch.setitem(row, key, value)
    with pytest.raises((AssertionError, KeyError)):
        test_all_capability_limits_and_unknown_conventions_are_consistent()


def test_capability_sources_really_match_the_frozen_main_revision():
    import ast
    for upstream, source in SOURCES["capabilities"].items():
        if source["source_ref"] is None:
            assert source["context_tokens"] == source["max_output_tokens"] == 0
            continue
        text = subprocess.check_output(["git", "show", source["source_ref"]], text=True)
        tree = ast.parse(text)
        call = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name) and node.target.id == "SEED_MODELS")
        models = ast.literal_eval(call.args[0])
        assert any(model["model_id"] in source["source_note"] and all(
            model[key] == source[key] for key in ("context_tokens", "max_output_tokens")) for model in models), upstream


@pytest.mark.parametrize("table,missing", [("catalog_models", "family"), ("catalog_benchmarks", "updated_at")])
def test_missing_required_seed_cells_fail_loudly(tmp_path, table, missing):
    with CatalogDB(tmp_path / "missing.db") as db:
        db.init_and_seed()
        row = dict((catalog_seed.SEED_MODELS if table == "catalog_models" else ROWS)[0])
        del row[missing]
        with pytest.raises(sqlite3.IntegrityError):
            db._insert_records(table, [row], seed=True)


def test_pinned_self_report_and_visual_score_are_not_claimed_authoritative():
    big = SOURCES["published"]["opencode/big-pickle"]
    assert "NOT authoritative" in big["source_note"] and "Self-reported single trial" in big["excerpt"]
    assert re.search(r"/blob/[a-f0-9]{40}/results/summary.json#L2-L10$", big["source_url"])
    for model, evidence in SOURCES["published"].items():
        if "muse-spark" in model:
            visual = evidence["visual_excerpt"]
            assert visual == {"column": "Muse Spark 1.3 (max)", "creator": "Meta", "row": "DeepSWE v1.1", "score": 75.4}
            assert evidence["source_sha256"] == SOURCES["publisher_assets"]["muse_scorecard_sha256"]


def test_fix_round_refreshes_every_previous_lane_row_without_operator_loss(tmp_path):
    old_rows = json.loads(subprocess.check_output([
        "git", "show", "54d06937:src/plangraph/routing_support/catalog_benchmarks.json"], text=True))
    with CatalogDB(tmp_path / "previous-lane.db") as db:
        db.init_and_seed()
        for row in old_rows:
            db._insert_records("catalog_benchmarks", [row])
        db.conn.execute("UPDATE catalog_benchmarks SET scicode=12.345 WHERE model_id='mimo/mimo-v2.6-pro'")
        db.init_and_seed()
        for row in ROWS:
            actual = db.get_model(row["model_id"], metric_view="benchmark")
            if row["model_id"] == "mimo/mimo-v2.6-pro":
                assert actual["scicode"] == 12.345
            else:
                for key, value in row.items():
                    if key != "updated_at":  # Joined get_model deliberately omits benchmark timestamp.
                        assert actual[key] == value, (row["model_id"], key)


def test_seed_foreign_key_violation_is_not_a_duplicate(tmp_path):
    with CatalogDB(tmp_path / "foreign.db") as db:
        db.init_and_seed()
        row = dict(ROWS[0], model_id="test/missing-model")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db._insert_records("catalog_benchmarks", [row], seed=True)


@pytest.mark.parametrize("wrong_current", [False, True])
def test_live_component_check_cannot_read_a_comparison_models_score(wrong_current):
    from test_bench_catalog_evidence import assert_live_source
    model = "omniroute/cx-gpt-6.1-sol-max"
    evidence = SOURCES["published"][model]
    component = json.loads(evidence["component_excerpt"])
    current = dict(component, slug="mimo-v2-6-pro") if wrong_current else component
    payload = json.dumps({"currentModel": current, "comparisonModels": [component]})
    content = (evidence["title"] + " " + evidence["excerpt"] +
               '<script>self.__next_f.push([1,' + json.dumps(payload) + '])</script>').encode()
    if wrong_current:
        with pytest.raises(AssertionError):
            assert_live_source(model, content)
    else:
        assert_live_source(model, content)
