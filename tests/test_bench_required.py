import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS
from plangraph.planner import routing

POLICY = tuple(l for l in DEFAULT_LEGS if set(l.roles) & {"implement", "review"})


def plan(**kwargs):
    return routing.plan_spawn({"estimate_tokens": 1000}, role="implement",
                              leg_reader=lambda *a, **k: {"state": "available", "evidence": None},
                              **kwargs)

WRITERS = tuple(plan().legs)


def rows(legs):
    return {l.model: {"model_id": l.model, "aa_intelligence": 45,
                      "cost_per_task_usd": 0, "verified_tok_per_s": 10} for l in legs}


@pytest.mark.parametrize("as_list", [False, True])
def test_missing_ranked_last_in_tier(as_list):
    data = rows(WRITERS[1:])
    p = plan(benchmark=list(data.values()) if as_list else data)
    assert p.gaps == ({"leg": WRITERS[0].id, "model": WRITERS[0].model,
                       "reason": "no benchmark row"},)
    assert any(WRITERS[0].id in r and "no benchmark row" in r for r in p.reasons)
    assert [l.id for l in p.legs if l.tier == "subscription"] == [
        "agy-gemini-3.8-flash", "mimo-v2.6-pro", "mimo-v2.6-flash", "cx-gpt-6.1-sol"]
    assert p.to_dict()["gaps"] == p.gaps
    assert not plan(benchmark=rows(WRITERS)).gaps


def test_absent_explicit_operator_order():
    p = plan()
    assert p.legs == WRITERS
    assert p.p_source == "operator-order"
    assert {g["leg"] for g in p.gaps} == {l.id for l in WRITERS}
    assert "benchmark data absent" in p.reasons


@pytest.mark.parametrize("data", [None, rows(WRITERS[1:])])
def test_strict_names_missing(data):
    with pytest.raises(routing.SpawnRefused, match="benchmark rows missing: .*opencode-deepseek-v4.1-flash-free"):
        plan(benchmark=data, strict=True)


def database(path, models, table="catalog_benchmarks"):
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE {table} (model_id TEXT, aa_intelligence REAL)")
        conn.executemany(f"INSERT INTO {table} VALUES (?, 45)", [(m,) for m in models])
    return path


@pytest.mark.parametrize("table", ["catalog_benchmarks", "model_benchmarks"])
def test_read_only_loader(tmp_path, table):
    path = database(tmp_path / "bench.db", [WRITERS[0].model], table)
    before = path.read_bytes()
    data, errors = routing.benchmark_rows_from_db(path)
    assert not errors and data[WRITERS[0].model]["aa_intelligence"] == 45
    assert path.read_bytes() == before


def test_missing_table_and_file(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    for p in (path, tmp_path / "missing.db"):
        data, errors = routing.benchmark_rows_from_db(p)
        assert data == {} and errors
        assert str(p) in errors[0]
    assert not (tmp_path / "missing.db").exists()


def gate(*paths, json_output=False):
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/bench_gate.py")]
    for path in paths:
        command.extend(["--db", str(path)])
    return subprocess.run(command + (["--json"] if json_output else []), capture_output=True, text=True)


def test_gate_complete_short_and_two_databases(tmp_path):
    models = list(dict.fromkeys(l.model for l in POLICY))
    missing_writer = WRITERS[0].model
    complete = database(tmp_path / "complete.db", models)
    short = database(tmp_path / "short.db", [m for m in models if m != missing_writer])
    assert gate(complete).returncode == 0
    result = gate(short)
    assert result.returncode == 1 and missing_writer in result.stdout
    result = gate(complete, short, json_output=True)
    assert result.returncode == 1
    evidence = json.loads(result.stdout)
    assert str(short) in result.stdout and missing_writer in result.stdout
    assert len(evidence["databases"]) == 2


def test_gate_unreadable(tmp_path):
    path = tmp_path / "bad.db"
    path.write_text("not sqlite")
    assert gate(path).returncode == 2
    assert gate(tmp_path / "missing.db").returncode == 2


def test_gate_import_guard():
    import runpy
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/bench_gate.py"), run_name="import_guard")


def test_review_gaps_include_filtered_policy_legs():
    reviewers = tuple(l for l in POLICY if "review" in l.roles)
    missing = reviewers[0]
    p = routing.plan_spawn({"estimate_tokens": 1000}, role="review",
                           writer_families=(missing.family,), benchmark=rows(reviewers[1:]),
                           leg_reader=lambda *a, **k: {"state": "available", "evidence": None})
    assert p.gaps == ({"leg": missing.id, "model": missing.model, "reason": "no benchmark row"},)
    with pytest.raises(routing.SpawnRefused, match=missing.id):
        routing.plan_spawn({"estimate_tokens": 1000}, role="review", strict=True,
                           writer_families=(missing.family,), benchmark=rows(reviewers[1:]))


def test_duplicate_rows_counted(tmp_path):
    model = WRITERS[0].model
    path = database(tmp_path / "duplicates.db", [model, model], "model_benchmarks")
    data, errors = routing.benchmark_rows_from_db(path)
    assert not errors and data[model]["benchmark_row_count"] == 2
