"""Host bench slice invariants: record host (C1), catalog DB path/provision (D1/D3),
leg-id -> benchmark model_id mapping (D2). All state stays under tmp_path."""
import json
import socket
from pathlib import Path

import pytest

from plangraph.planner.routing import SpawnRefused
from plangraph.routing_support import catalog_db
from plangraph.spawner.bench_entry import benchmark_options
from plangraph.spawner.service import SpawnService

CARD = {"estimate_tokens": 1000}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Pin HOME and catalog/state paths into tmp; never touch ~/.cache or ~/.local/state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("PLANGRAPH_HOST", raising=False)
    default_db = tmp_path / "spawner_catalog.db"
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(default_db))
    monkeypatch.setattr(catalog_db, "DEFAULT_DB_PATH", default_db)


def test_record_host_field(tmp_path, monkeypatch):
    """I1/C1: every new record carries host; PLANGRAPH_HOST overrides; old records load."""
    service = SpawnService(state=tmp_path / "state")
    record = service.save_refusal(SpawnRefused("host field probe"))
    stored = service.result(record["run_id"])
    assert stored["host"] == socket.gethostname()
    assert json.loads(service._path(record["run_id"]).read_text())["host"] == socket.gethostname()
    # A spawn-shaped record is stamped at the same write site.
    run_id = "run-" + "a1b2c3d4"
    service._save(dict(run_id=run_id, role="implement", status="running", started=1.0,
                       ended=None, attempts=[], stdout_tail="", exit_code=None,
                       cwd=str(tmp_path)))
    assert service.result(run_id)["host"] == socket.gethostname()
    # PLANGRAPH_HOST overrides; no other env does.
    monkeypatch.setenv("HOSTNAME", "not-the-host")
    monkeypatch.setenv("PLANGRAPH_HOST", "bench-host-override")
    stamped = service.save_refusal(SpawnRefused("override probe"))
    assert service.result(stamped["run_id"])["host"] == "bench-host-override"
    monkeypatch.delenv("PLANGRAPH_HOST")
    plain = service.save_refusal(SpawnRefused("no override"))
    assert service.result(plain["run_id"])["host"] == socket.gethostname()
    # Old records without a host still read via result().
    old_id = "run-oldrecord01"
    path = service._path(old_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": old_id, "status": "completed"}))
    old = service.result(old_id)
    assert old["run_id"] == old_id and "host" not in old


def test_catalog_db_path_both_orders(tmp_path):
    """I2/D1: a global --db-path reaches the subcommand in either position."""
    default = tmp_path / "spawner_catalog.db"
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    assert catalog_db.main(["--db-path", str(first), "init-and-seed"]) == 0
    assert catalog_db.main(["init-and-seed", "--db-path", str(second)]) == 0
    assert first.is_file() and second.is_file()
    assert not default.exists()


def seeded_db(tmp_path):
    """Provision a seeded catalog DB through the CLI (D3 provision path)."""
    db = tmp_path / "seeded.db"
    assert catalog_db.main(["--db-path", str(db), "init-and-seed"]) == 0
    return db


def route_json(tmp_path, capsys, *extra):
    from plangraph.spawner.cli import main as cli_main
    capsys.readouterr()  # drain earlier prints; only the route JSON is parsed
    assert cli_main(["--state", str(tmp_path / "state"), "route", "--card", json.dumps(CARD),
                     "--role", "review", "--writer-family", "claude", "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_review_route_uses_benchmark_p(tmp_path, capsys):
    """I3/D2: seeded rows reach review ranking through the leg-id map."""
    db = seeded_db(tmp_path)
    rows = benchmark_options(False, str(db))["benchmark"]
    assert {"qoder/qwen3.8-flash-next", "agy/gemini-3.8-flash-tiered-high",
            "agy/claude-sonnet-5-5-medium", "mimo/mimo-v2.6-pro",
            "mimo/mimo-v2.6-flash"} <= set(rows)
    plan = route_json(tmp_path, capsys, "--bench-db", str(db))
    assert plan["p_source"] == "benchmark"
    by_leg = {c["leg"]: c for c in plan["candidates"]}
    for leg in ("qodercli-qwen3.8-flash", "agy-gemini-3.8-flash", "mimo-v2.6-flash"):
        assert by_leg[leg]["u_core"] is not None, leg
        assert by_leg[leg]["p_source"] == "measured", leg
    # Without a DB: no p anywhere and the order is today's operator order.
    bare = route_json(tmp_path, capsys)
    assert bare["p_source"] == "operator-order"
    assert all(c["u_core"] is None and c["p_source"] == "prior" for c in bare["candidates"])
    # luna and mimo-pro are inline-only: skipped while the card has no diff_ref.
    assert [leg["id"] for leg in bare["legs"]] == [
        "qodercli-qwen3.8-flash", "opencode-deepseek-v4.1-flash-free",
        "opencode-glm-5.3-flash-free", "agy-gemini-3.8-flash",
        "mimo-v2.6-flash"]


def test_unmapped_leg_no_p(tmp_path, capsys):
    """I4/D2: a leg missing from the map/DB gets no p (mapped leg: positive control)."""
    from plangraph.spawner.bench_entry import LEG_BENCHMARK_MODEL
    db = seeded_db(tmp_path)
    rows = benchmark_options(False, str(db))["benchmark"]
    for leg_id, model_id in LEG_BENCHMARK_MODEL.items():
        assert rows[leg_id]["model_id"] == model_id
    plan = route_json(tmp_path, capsys, "--bench-db", str(db))
    by_leg = {c["leg"]: c for c in plan["candidates"]}
    for leg in ("opencode-glm-5.3-flash-free", "opencode-deepseek-v4.1-flash-free"):
        assert leg not in LEG_BENCHMARK_MODEL
        assert by_leg[leg]["u_core"] is None and by_leg[leg]["p_source"] == "prior"
    assert by_leg["agy-gemini-3.8-flash"]["p_source"] == "measured"


def test_implement_order_unchanged_by_benchmark(tmp_path):
    """D-2683: implement ignores benchmark p; ordering stays operator order."""
    from plangraph.planner.routing import plan_spawn
    db = seeded_db(tmp_path)
    rows = benchmark_options(False, str(db))["benchmark"]
    with_bench = plan_spawn(CARD, role="implement", benchmark=rows).to_dict()
    bare = plan_spawn(CARD, role="implement").to_dict()
    assert with_bench["p_source"] == "operator-order" == bare["p_source"]
    assert [leg["id"] for leg in with_bench["legs"]] == [leg["id"] for leg in bare["legs"]]


def test_seed_idempotent(tmp_path):
    """I5/D3: bare init-and-seed provisions the default DB; re-run = same rows."""
    default = tmp_path / "spawner_catalog.db"
    assert catalog_db.main(["init-and-seed"]) == 0
    assert default.is_file()

    def counts():
        with catalog_db.CatalogDB(default) as db:
            tables = ("catalog_quota_pools", "catalog_providers", "catalog_clients",
                      "catalog_models", "catalog_benchmarks", "catalog_routes",
                      "catalog_route_legs")
            return {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in tables}

    first = counts()
    assert all(first.values())
    assert catalog_db.main(["init-and-seed"]) == 0
    assert counts() == first


def test_record_host_never_redacted(tmp_path, monkeypatch):
    """D-2960: host is an identifier; an env value equal to it never redacts it."""
    service = SpawnService(state=tmp_path / "state")
    monkeypatch.setenv("PLANGRAPH_HOST", "bench-host-secretlike")
    monkeypatch.setenv("HOSTNAME", "bench-host-secretlike")
    monkeypatch.setenv("AUTH_HOST_TOKEN", "bench-host-secretlike")
    record = service.save_refusal(SpawnRefused("host redaction probe"))
    assert service.result(record["run_id"])["host"] == "bench-host-secretlike"
    # Positive control: the same value elsewhere in the record is still redacted.
    refused = service.save_refusal(SpawnRefused("leak bench-host-secretlike"))
    assert "bench-host-secretlike" not in json.dumps({k: v for k, v in service.result(refused["run_id"]).items() if k != "host"})


def test_nested_host_key_still_redacted(tmp_path, monkeypatch):
    """D-2960 seat MED: only the top-level run host is exempt; a nested 'host' is cleaned."""
    service = SpawnService(state=tmp_path / "state")
    monkeypatch.setenv("PLANGRAPH_HOST", "bench-host-secretlike")
    monkeypatch.setenv("AUTH_HOST_TOKEN", "bench-host-secretlike")
    run_id = "run-" + "nestedhost01"
    service._save(dict(run_id=run_id, role="implement", status="running", started=1.0, ended=None,
                       attempts=[{"leg": {"id": "x", "host": "bench-host-secretlike"}}],
                       stdout_tail="", exit_code=None, cwd=str(tmp_path)))
    stored = service.result(run_id)
    assert stored["host"] == "bench-host-secretlike"
    assert "bench-host-secretlike" not in json.dumps(stored["attempts"])
