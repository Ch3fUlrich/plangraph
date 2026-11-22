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


def test_review_route_uses_benchmark_p(tmp_path, capsys, monkeypatch):
    """I3/D2: seeded rows reach review ranking through the leg-id map."""
    from plangraph.planner.routing import plan_spawn
    from test_route_round3 import test_review_same_family_table_order
    # Pin CLI tier/admission inputs independently of the host catalog snapshot.
    monkeypatch.setitem(plan_spawn.__kwdefaults__, "catalog_rows", ())
    test_review_same_family_table_order()
    db = seeded_db(tmp_path)
    rows = benchmark_options(False, str(db))["benchmark"]
    assert {"qoder/qwen3.8-flash-next", "agy/gemini-3.8-flash-tiered-high",
            "agy/claude-sonnet-5-5-medium", "mimo/mimo-v2.6-pro",
            "mimo/mimo-v2.6-flash"} <= set(rows)
    plan = route_json(tmp_path, capsys, "--bench-db", str(db))
    assert plan["p_source"] == "operator-order"
    by_leg = {c["leg"]: c for c in plan["candidates"]}
    for leg in ("agy-sonnet-5.5", "mimo-v2.6-flash"):
        assert by_leg[leg]["u_core"] is not None, leg
        assert by_leg[leg]["p_source"] == "measured", leg
    # Without a DB: no p anywhere and the order is today's operator order.
    bare = route_json(tmp_path, capsys)
    assert bare["p_source"] == "operator-order"
    assert all(c["u_core"] is None and c["p_source"] == "prior" for c in bare["candidates"])
    from plangraph.planner.legs import REVIEW_ORDER
    assert [leg["id"] for leg in bare["legs"]] == [
        *[id for id in REVIEW_ORDER if id not in ("claude-sonnet-5-5-high", "mimo-v2.6-pro", "mimo-v2.6-flash",
                                                  "agy-gemini-4-argon-low", "agy-gemini-4-argon-medium")],
        "agy-gpt-oss-120b", "agy-gemini-4-argon-low", "agy-gemini-4-argon-medium",   # argon is last
    ]


def test_review_route_ignores_ambient_catalog_tiers(tmp_path, capsys, monkeypatch):
    """The exact CLI order uses declared tiers even if the host catalog disagrees."""
    from plangraph.routing_support import resolver

    reads = []
    def ambient_catalog():
        reads.append(1)
        return [{'id': 'agy/gemini-4-neon-medium', 'tier': 'paid', 'available': 1, 'active': True}]

    monkeypatch.setattr(resolver, 'get_catalog_models', ambient_catalog)
    ambient = route_json(tmp_path, capsys)
    assert reads == [1]
    assert next(c for c in ambient['candidates'] if c['leg'] == 'agy-gemini-4-neon-medium')['tier'] == 'paid'
    assert ambient['first_leg']['id'] == 'agy-gemini-4-hydrogen-high'
    test_review_route_uses_benchmark_p(tmp_path, capsys, monkeypatch)
    assert reads == [1]  # The exact-order probe must inject its own catalog input.


def test_unmapped_leg_no_p(tmp_path, capsys, monkeypatch):
    """I4/D2: a leg missing from the map/DB gets no p (mapped leg: positive control)."""
    from plangraph.spawner.bench_entry import LEG_BENCHMARK_MODEL
    db = seeded_db(tmp_path)
    rows = benchmark_options(False, str(db))["benchmark"]
    for leg_id, model_id in LEG_BENCHMARK_MODEL.items():
        assert rows[leg_id]["model_id"] == model_id
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import plan_spawn
    control = next(leg for leg in DEFAULT_LEGS if leg.id == 'agy-sonnet-5.5')
    synthetic = replace(control, id='synthetic-unmapped-review', model='synthetic/no-row')
    from plangraph.routing_support import resolver
    catalog_models = resolver.get_catalog_models
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        *catalog_models(), {'id': synthetic.model, 'available': 1, 'active': True}])
    assert synthetic.id not in rows and synthetic.model not in rows
    plan = plan_spawn(CARD, role='review', writer_families=('claude',),
                      legs=(synthetic, control), benchmark=rows).to_dict()
    by_leg = {c["leg"]: c for c in plan["candidates"]}
    for leg in (synthetic.id,):
        assert leg not in LEG_BENCHMARK_MODEL
        assert by_leg[leg]["u_core"] is None and by_leg[leg]["p_source"] == "prior"
    assert by_leg["agy-sonnet-5.5"]["p_source"] == "measured"


def test_implement_order_unchanged_by_benchmark(tmp_path):
    """D-5212 pins Sol/Gemini; D-5092 utility stays within all remaining cost tiers."""
    from plangraph.planner.routing import plan_spawn
    db = seeded_db(tmp_path)
    rows = benchmark_options(False, str(db))["benchmark"]
    with_bench = plan_spawn(CARD, role="implement", benchmark=rows).to_dict()
    bare = plan_spawn(CARD, role="implement").to_dict()
    assert with_bench["p_source"] == "benchmark" and bare["p_source"] == "operator-order"
    assert {leg["id"] for leg in with_bench["legs"]} == {leg["id"] for leg in bare["legs"]}
    from plangraph.planner.legs import TIERS
    for plan in (with_bench, bare):
        assert [l["id"] for l in plan["legs"][:6]] == [
            "cx-gpt-6.2-sol-medium", "cx-gpt-6.2-astra", "agy-gemini-4-hydrogen-high",
            "opencode-deepseek-v4.1-flash-free", "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash",
        ]
        assert plan["first_leg"]["effort"] == "medium"
        tiers = [TIERS.index(l["tier"]) for l in plan["legs"][6:]]
        assert tiers == sorted(tiers)


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


def head_card(tmp_path, monkeypatch, writer_line):
    """A card whose diff_ref head commit carries ``writer_line`` as its Writer trailer."""
    import subprocess
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = tmp_path / "repo"
    repo.mkdir()
    ident = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (repo / "f.txt").write_text("a\n")
    git("add", "f.txt")
    git(*ident, "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "f.txt").write_text("b\n")
    git(*ident, "commit", "-qam", f"change\n\nWriter: {writer_line}")
    return dict(CARD, diff_ref={"repo": str(repo), "base": base, "head": git("rev-parse", "HEAD")})


def families_served(plan):
    return {leg["family"] for leg in plan["legs"]}


def test_review_head_writer_trailer_excludes_its_family_despite_other_exclusion(tmp_path, capsys, monkeypatch):
    """The 2026-11-19 incident: --writer-family mimo (meant 'review on mimo') plus a gemini writer head
    must never serve a gemini leg."""
    from plangraph.spawner.cli import main as cli_main
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    capsys.readouterr()
    assert cli_main(["--state", str(tmp_path / "state"), "route", "--card", json.dumps(card),
                     "--role", "review", "--exclude-family", "mimo", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["legs"] and "gemini" not in families_served(plan) and "mimo" not in families_served(plan)
    assert any(row["reason"] == "excluded_family" and "gemini" in row["leg"] for row in plan["skipped"])


def test_review_head_writer_trailer_alone_names_the_writer(tmp_path, capsys, monkeypatch):
    from plangraph.spawner.cli import main as cli_main
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    capsys.readouterr()
    assert cli_main(["--state", str(tmp_path / "state"), "route", "--card", json.dumps(card),
                     "--role", "review", "--exclude-family", "claude", "--json"]) == 0
    assert "gemini" not in families_served(json.loads(capsys.readouterr().out))


def test_review_unknown_head_writer_trailer_refuses(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    card = head_card(tmp_path, monkeypatch, "no-such-model-xyz run-1 (writer)")
    with pytest.raises(SpawnRefused, match="Writer trailer"):
        SpawnService(state=tmp_path / "state").route(card, "review", ["claude"])


def test_explicit_leg_of_head_writer_family_is_refused(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner.service import SpawnService
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    service = SpawnService(state=tmp_path / "state")
    with pytest.raises(SpawnRefused, match="excluded writer family"):
        asyncio.run(service._spawn(card=card, role="review", writer_families=["mimo"],
                                   leg="agy-gemini-4-neon-medium", dry_run=True))


def test_exclude_family_flag_and_deprecated_writer_family_alias(tmp_path, capsys):
    from plangraph.spawner.cli import main as cli_main
    state = str(tmp_path / "state")
    capsys.readouterr()
    assert cli_main(["--state", state, "route", "--card", json.dumps(CARD), "--role", "review",
                     "--exclude-family", "claude", "--json"]) == 0
    new = capsys.readouterr()
    assert "deprecated" not in new.err
    assert cli_main(["--state", state, "route", "--card", json.dumps(CARD), "--role", "review",
                     "--writer-family", "claude", "--json"]) == 0
    old = capsys.readouterr()
    assert "--writer-family is deprecated" in old.err and "--exclude-family" in old.err
    assert json.loads(old.out)["legs"] == json.loads(new.out)["legs"]


def test_explicit_leg_of_any_head_writer_family_is_refused_with_several_writers(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner.service import SpawnService
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    import subprocess
    repo = card["diff_ref"]["repo"]
    subprocess.run(["git", "-C", repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q",
                    "--amend", "-m", "change\n\nWriter: gemini-4-hydrogen-high run-1 (writer)\nWriter: claude-sonnet-5-5 s (writer)"],
                   check=True, capture_output=True)
    card["diff_ref"]["head"] = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True,
                                              capture_output=True, text=True).stdout.strip()
    with pytest.raises(SpawnRefused, match="excluded writer family"):
        asyncio.run(SpawnService(state=tmp_path / "state")._spawn(
            card=card, role="review", writer_families=["mimo"], leg="agy-gemini-4-neon-medium", dry_run=True))


def test_unreadable_review_head_refuses_instead_of_assuming_no_writer(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    card["diff_ref"]["head"] = "0" * 40
    with pytest.raises(SpawnRefused, match="cannot read the Writer trailer"):
        SpawnService(state=tmp_path / "state").route(card, "review", ["claude"])


@pytest.mark.parametrize("dispatch", [False, True])
def test_trailer_family_is_recorded_as_given_writer_family(tmp_path, monkeypatch, dispatch):
    import asyncio
    from registry_route_util import _Runner
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import spawn_service as DispatchService
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    service = (DispatchService if dispatch else SpawnService)(_Runner(), state=tmp_path / "state")
    plan = SpawnPlan((next(l for l in DEFAULT_LEGS if l.family == "qwen"),), (), "test", (), ())
    monkeypatch.setattr(service, "_route", lambda *a, **kw: plan)
    monkeypatch.setattr(service, "_leg_prompt", lambda *a, **kw: "review")

    async def identity(plan, **kwargs):
        return plan
    monkeypatch.setattr("plangraph.spawner.resolver.plan_for_launch_async", identity)
    record = asyncio.run(service.spawn(card=card, role="review", writer_families=["mimo"], dry_run=True))
    assert record["given_writer_families"] == ["mimo", "gemini-4-hydrogen-high"]


def test_routed_host_skip_failure_also_records_the_skipped_field(tmp_path, monkeypatch):
    import asyncio
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import spawn_service as DispatchService
    from registry_route_util import _Runner, CARD as ROUTE_CARD
    service = DispatchService(_Runner(), state=tmp_path / "state")
    skipped = ({"leg": "a", "reason": "not_served_on_host: m", "skip_reason": "not_served_on_host: m"},)
    plan = SpawnPlan((), skipped, "t", (), ())
    monkeypatch.setattr(service, "_route", lambda *a, **kw: plan)
    monkeypatch.setattr(service, "_leg_prompt", lambda *a, **kw: "p")
    record = asyncio.run(service.spawn(card=ROUTE_CARD, role="implement", dry_run=False))
    assert "not_served_on_host" in record["error"]   # a failed run keeps its host reason
    assert record["skipped"][0]["leg"] == "a"


def test_seat_refusal_keeps_the_skip_list_in_its_own_field(tmp_path, monkeypatch):
    import asyncio
    from plangraph.planner.routing import SpawnPlan
    from registry_route_util import _Runner, CARD as ROUTE_CARD
    service = SpawnService(_Runner(), state=tmp_path / "state")
    skipped = ({"leg": "a", "reason": "excluded_family"},)
    monkeypatch.setattr(service, "_route", lambda *a, **kw: SpawnPlan((), skipped, "t", (), (), error="no eligible leg"))
    with pytest.raises(SpawnRefused):
        asyncio.run(service.spawn(card=ROUTE_CARD, role="review", writer_families=["claude"], seats=True, dry_run=True))
    record = service.ps()["runs"][0]
    assert record["error"] == "no eligible leg" and record["skipped"] == list(skipped)


@pytest.mark.parametrize("head", ["f.txt", "HEAD..HEAD", "HEAD~1..HEAD", "HEAD...HEAD", "HEAD^@"])
def test_head_that_is_not_a_single_commit_is_refused(tmp_path, monkeypatch, head):
    from plangraph.planner.seats import head_writer_families
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    card["diff_ref"]["head"] = head   # a tracked path, a range or a negation: never a commit to read
    with pytest.raises(SpawnRefused, match="cannot read the Writer trailer"):
        head_writer_families(card)


def test_flag_and_review_of_families_are_both_recorded(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    service = SpawnService(state=tmp_path / "state")
    source = service.save_refusal(SpawnRefused("x"))   # any saved run
    run_id = "run-" + "c" * 12
    service._save(dict(run_id=run_id, role="implement", status="completed", started=1.0, ended=2.0,
                       attempts=[], stdout_tail="", exit_code=0, cwd=str(tmp_path),
                       given_writer_families=["openai"], writer_family="openai"))
    assert service._given_writers(CARD, "review", ["mimo"], run_id) == ["mimo", "openai"]
    assert service._given_writers(CARD, "review", ["mimo"], None) == ["mimo"]


@pytest.mark.parametrize("dispatch", [False, True])
def test_flag_review_of_and_trailer_families_all_reach_the_spawned_record(tmp_path, monkeypatch, dispatch):
    import asyncio
    from registry_route_util import _Runner
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import spawn_service as DispatchService
    card = head_card(tmp_path, monkeypatch, "gemini-4-hydrogen-high run-1 (writer)")
    service = (DispatchService if dispatch else SpawnService)(_Runner(), state=tmp_path / "state")
    run_id = "run-" + "d" * 12
    service._save(dict(run_id=run_id, role="implement", status="completed", started=1.0, ended=2.0,
                       attempts=[], stdout_tail="", exit_code=0, cwd=str(tmp_path),
                       given_writer_families=["openai"], writer_family="openai"))
    plan = SpawnPlan((next(l for l in DEFAULT_LEGS if l.family == "qwen"),), (), "test", (), ())
    seen = []
    monkeypatch.setattr(service, "_route", lambda *a, **kw: seen.append(a) or plan)
    monkeypatch.setattr(service, "_leg_prompt", lambda *a, **kw: "review")

    async def identity(plan, **kwargs):
        return plan
    monkeypatch.setattr("plangraph.spawner.resolver.plan_for_launch_async", identity)
    record = asyncio.run(service.spawn(card=card, role="review", writer_families=["mimo"],
                                       review_of=run_id, dry_run=True))
    assert record["given_writer_families"] == ["mimo", "openai", "gemini-4-hydrogen-high"]
    # The launch itself carries all three excluded families in its routing headers.
    headers = service.runner.calls[-1]["headers"]
    assert {"mimo", "gpt", "gemini"} <= {value for key, value in headers.items() if "exclude" in key.lower()
                                         for value in str(value).split(",")}, headers


def test_plain_spawn_refusal_keeps_the_skip_list_in_its_own_field(tmp_path, monkeypatch):
    import asyncio
    from plangraph.planner.routing import SpawnPlan
    from registry_route_util import _Runner, CARD as ROUTE_CARD
    service = SpawnService(_Runner(), state=tmp_path / "state")
    skipped = ({"leg": "a", "reason": "excluded_family"},)
    monkeypatch.setattr(service, "_route", lambda *a, **kw: SpawnPlan((), skipped, "t", (), (), error="no eligible leg"))
    with pytest.raises(SpawnRefused):
        asyncio.run(service.spawn(card=ROUTE_CARD, role="review", writer_families=["claude"], dry_run=True))
    record = service.ps()["runs"][0]
    assert record["error"] == "no eligible leg" and record["skipped"] == list(skipped)
