from dataclasses import replace
from pathlib import Path

from plangraph.planner.legs import DEFAULT_LEGS, REVIEW_REQUIRED
from plangraph.planner.route_policy import load_route_policy
from plangraph.planner.routing import plan_spawn, route_card


REQUIRED_MODELS = {leg.model for leg in DEFAULT_LEGS if leg.id in REVIEW_REQUIRED}


def test_qoder_off_for_review_even_with_old_active_leg():
    leg = next(l for l in DEFAULT_LEGS if l.harness == 'qodercli')
    policy = load_route_policy(Path('src/plangraph/planner/route_policy.json'))
    assert policy.skip_reason(replace(leg, disabled=None), 'review') == 'paused_harness'


def test_disabled_leg_never_a_route_option():
    disabled = next(l for l in DEFAULT_LEGS if l.harness == 'qodercli')
    later = next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-4-neon-medium')
    route = route_card({'estimate_tokens': 1000}, legs=(disabled, later), role='review')
    assert route.legs == (later,)
    assert 'excluded 1 legs: disabled' in route.reasons


def test_large_review_falls_through_small_window_with_reason():
    small = next(l for l in DEFAULT_LEGS if l.id == 'opencode-deepseek-v4.1-flash-free')
    large = next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-4-neon-medium')
    route = route_card({'estimate_tokens': 200000, 'window_class': '128k'},
                       legs=(small, large), role='review')
    assert route.legs == (large,)
    assert route.window_class == '1m'
    assert 'excluded 1 legs: window_class' in route.reasons


def test_review_route_never_offers_qwen_max():
    route = plan_spawn({'estimate_tokens': 1000}, role='review', writer_families=('gpt',), catalog_rows=())
    assert 'opencode-go-qwen3.8-max' not in [leg.id for leg in route.legs]


def test_glm_is_an_admitted_review_option():
    policy = load_route_policy(Path('src/plangraph/planner/route_policy.json'))
    assert 'glm' not in policy.off_families
    route = plan_spawn({'estimate_tokens': 1000}, role='review',
                       writer_families=('gpt',), policy=policy, catalog_rows=())
    assert 'opencode-go-glm-5.3-flash' in {leg.id for leg in route.legs}
    assert {s['leg']: s['reason'] for s in route.skipped}['opencode-big-pickle'] == 'low_risk_only'


def test_d6550_routes_native_writer_hydrogen_and_sensitive_review():
    def available(model, **kwargs):
        return {"state": "available", "evidence": None}

    card = {"estimate_tokens": 1000}

    def plan(role, data=card, reader=available):
        return plan_spawn(data, role=role, writer_families=("gpt",) if role == "review" else (),
                          leg_reader=reader, catalog_rows=())

    writer = plan("implement")
    review = plan("review")
    sensitive = plan("review", dict(card, paths=["src/auth/session.rs"]))
    assert (writer.first_leg.id, writer.first_leg.harness) == ("cx-gpt-6.2-sol-medium", "codex")
    assert all(not leg.claude_family for leg in writer.legs)
    assert [leg.id for leg in review.review_seats] == list(REVIEW_REQUIRED)
    assert all(not leg.claude_family for leg in review.legs)
    assert sensitive.first_leg.id == "claude-sonnet-5-5-high"

    def limited(model, **kwargs):
        state = available(model)
        if model in {"gpt-6.2-sol-medium", "gemini-4-hydrogen-high", "omniroute/cx-gpt-6.2-sol-medium"}:
            state["state"] = "limited"
        return state

    def required_out(model, **kwargs):
        state = available(model)
        if model in REQUIRED_MODELS:
            state["state"] = "limited"
        return state

    assert not any(leg.claude_family for leg in plan("review", reader=limited).legs)
    assert any(leg.claude_family for leg in plan("review", reader=required_out).legs)
    limited_writer = plan("implement", reader=limited)
    assert limited_writer.first_leg.id == "claude-sonnet-5-5-high"   # Codex limited: Sonnet high writes
    assert any(leg.id == "agy-claude-sonnet-5-5-high" for leg in limited_writer.legs)
    assert not any(leg.id == "agy-claude-sonnet-5-5-high" for leg in writer.legs)


def test_d6550_claude_fallback_reads_booked_provider_quotas(tmp_path):
    from plangraph.routing_support.leg_state import LegStateStore

    for gpt_key in ("omniroute/cx-gpt-6.2-sol-medium", "cx-gpt-6.2-sol-medium",
                    "gpt-6.2-sol-medium"):
        store = LegStateStore(tmp_path / (gpt_key.replace("/", "-") + ".sqlite"))

        def fallback_admitted(role, *, agy_installed=True):
            writers = ("gpt",) if role == "review" else ()
            plan = plan_spawn({"estimate_tokens": 1000}, role=role, writer_families=writers,
                              leg_reader=store, now=100, catalog_rows=(),
                              installed=lambda harness: agy_installed or harness != "agy")
            return any(leg.claude_family for leg in plan.legs)

        def agy_writer_admitted():
            plan = plan_spawn({"estimate_tokens": 1000}, role="implement", leg_reader=store, now=100,
                              catalog_rows=(), installed=lambda harness: True)
            return any(leg.id == "agy-claude-sonnet-5-5-high" for leg in plan.legs)

        store.record_provider_response(gpt_key, 429, "quota", {"Retry-After": "60"}, "fixture", now=100)
        assert not agy_writer_admitted()
        assert not fallback_admitted("review")
        store.record_provider_response("agy/gemini-4-hydrogen-high", 429, "quota",
                                       {"Retry-After": "60"}, "fixture", now=100)
        assert agy_writer_admitted(), gpt_key   # the agy Sonnet fallback keeps the dual quota guard
        assert not fallback_admitted("review"), gpt_key   # D-6652: Claude review needs both required seats out


def test_d6550_spawn_dry_run_reads_credential_diff(tmp_path, monkeypatch, capsys):
    import json
    import subprocess

    from plangraph.spawner.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", *args],
                       check=True, capture_output=True)

    git("init")
    git("commit", "--allow-empty", "-m", "base")
    (repo / "ordinary.rs").touch()
    git("add", "ordinary.rs")
    git("commit", "-m", "ordinary diff")
    (repo / "credential_store.rs").touch()
    git("add", "credential_store.rs")
    git("commit", "-m", "credential diff")
    monkeypatch.setenv("PLANGRAPH_OWN_HARNESS", "off")

    for role, base, head, expected in (
        ("implement", None, None, ("cx-gpt-6.2-sol-medium", "codex")),
        ("review", "HEAD~2", "HEAD~1", ("opencode-go-deepseek-v4-flash", "opencode")),
        ("review", "HEAD~1", "HEAD", ("claude-sonnet-5-5-high", "claude")),
    ):
        card = {"estimate_tokens": 1000}
        if base:
            card["diff_ref"] = {"repo": str(repo), "base": base, "head": head}
        args = ["--state", str(tmp_path / "state"), "spawn", "--dry-run", "--json",
                "--card", json.dumps(card), "--role", role, "routing test"]
        if role == "review":
            args.extend(("--writer-family", "gpt"))
        assert main(args) == 0
        selected = json.loads(capsys.readouterr().out)["chosen_leg"]
        assert (selected["id"], selected["harness"]) == expected


def test_native_review_route_requires_repository_tools():
    plan = plan_spawn({"estimate_tokens": 1000, "repository_review_required": True},
                      role="review", writer_families=("gpt",), installed=lambda _: True,
                      leg_reader=lambda *_args, **_kwargs: {"state": "available", "evidence": None},
                      catalog_rows=())
    assert plan.first_leg.id == "opencode-go-deepseek-v4-flash"
    assert all(leg.tool_mode == "tools" for leg in plan.legs)
    assert {leg.id for leg in plan.legs if leg.harness == "opencode"} == {
        "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash",
    }


def test_native_review_cli_preview_retains_opencode_repository_legs(tmp_path, monkeypatch, capsys):
    import json

    from plangraph.planner import legs
    from plangraph.spawner.cli import main

    monkeypatch.delenv("PLANGRAPH_OWN_HARNESS", raising=False)
    monkeypatch.setattr(legs, "client_installed", lambda name: name == "opencode")
    card = {"estimate_tokens": 1000, "repository_review_required": True}
    assert main(["--state", str(tmp_path), "route", "--role", "review", "--writer-family", "gpt",
                 "--json", "--card", json.dumps(card)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert {leg["id"] for leg in plan["legs"]} == {
        "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash",
    }
    reasons = {row["leg"]: row["reason"] for row in plan["skipped"]}
    assert reasons["agy-gemini-4-hydrogen-high"] == "client_missing"


def test_native_review_cli_preview_reports_empty_route_with_causes(tmp_path, monkeypatch, capsys):
    import json

    from plangraph.planner import legs, route_policy
    from plangraph.spawner.cli import main

    monkeypatch.delenv("PLANGRAPH_OWN_HARNESS", raising=False)
    monkeypatch.setattr(legs, "client_installed", lambda name: name == "opencode")
    monkeypatch.setattr(route_policy, "opencode_repository_review", lambda _: False)
    card = {"estimate_tokens": 1000, "repository_review_required": True}
    assert main(["--state", str(tmp_path), "route", "--role", "review", "--writer-family", "gpt",
                 "--json", "--card", json.dumps(card)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["legs"] == []
    reasons = {row["leg"]: row["reason"] for row in plan["skipped"]}
    assert reasons["agy-gemini-4-hydrogen-high"] == "client_missing"
    assert reasons["opencode-go-deepseek-v4-flash"] == "repository_review_tools_unavailable"


def test_counted_seat_canary_holds_fourth_lane_until_promotion(tmp_path, monkeypatch, capsys):
    import asyncio
    import json
    from types import SimpleNamespace

    import pytest
    from keeper_util import World
    import merge_batch_land
    from plangraph.keeper.errors import Refused
    from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
    from plangraph.planner import legs
    from plangraph.routing_errors import SpawnRefused
    from plangraph.spawner.cli import main
    from plangraph.spawner.mcp import MCPSpawnerServer

    monkeypatch.setenv("PLANGRAPH_OWN_HARNESS", "off")
    monkeypatch.setattr(legs, "client_installed", lambda _name: True)
    old = "opencode-go-deepseek-v4-flash"
    new = "opencode-go-glm-5.3-flash"
    heads = [f"{i:040x}" for i in range(1, 5)]
    lanes = [f"lane-{i}" for i in range(1, 5)]
    policy = json.loads(Path("src/plangraph/planner/route_policy.json").read_text())
    policy["counted_review"] = {
        "previous": {"revision": "old", "leg": old},
        "candidate": {"revision": "new", "leg": new},
        "cohort": lanes[:3],
        "heads": dict(zip(heads, lanes)),
    }
    path = tmp_path / "route.json"
    path.write_text(json.dumps(policy))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))

    def route(head):
        card = {"estimate_tokens": 1000, "head_sha": head}
        result = main(["--state", str(tmp_path), "route", "--role", "review", "--exclude-family",
                       "gpt", "--json", "--card", json.dumps(card)])
        captured = capsys.readouterr()
        assert result == 0, captured.err or captured.out
        return [leg["id"] for leg in json.loads(captured.out)["legs"]]

    def mcp(head, *, leg=None):
        state = tmp_path / "mcp"
        server = MCPSpawnerServer(state=state)
        args = dict(card={"estimate_tokens": 1000, "head_sha": head}, role="review",
                    writer_families=["gpt"])
        if leg:
            args.update(prompt="fixture", dry_run=True, leg=leg)
        verb = "spawn" if leg else "route"
        return asyncio.run(server.call_tool("plangraph_agent_" + verb, args))

    def receipt(head, index, is_candidate, run, *, accepted=True):
        seat, revision = (new, "new") if is_candidate else (old, "old")
        return dict(binding=dict(selection=dict(seat=dict(revision=revision, leg=seat), lane=lanes[index]),
                    head=head, run_id=run, item_key="demo:A", diff_lines=130, writer_families=["openai"]),
                    accepted=accepted, round=1 if accepted else None)

    def mirror(row):
        binding = row["binding"]
        seat = binding["selection"]["seat"]["leg"]
        model = "zai/glm-5.3-flash" if seat == new else "deepseek/deepseek-v4-flash"
        run = binding["run_id"]
        location = tmp_path / "mirror" / "fixture" / run
        location.mkdir(mode=0o700, parents=True)
        location.parent.chmod(0o700)
        location.parent.parent.chmod(0o700)
        path = location / "record.json"
        path.write_text(json.dumps({"run_id":run, "host":"fixture", "item_key":"demo:A",
            "role":"review", "status":"completed", "exit_code":0, "served_model":model,
            "finish_reason":"stop", "head_sha":binding["head"], "completion":{"head_sha":binding["head"]},
            "attempts":[{"status":"completed","leg":{"id":seat},"served_model":model}],
            "stdout_tail":"FILES READ: src/lib.rs\nTEST_GAMING: no\nVERDICT: ACCEPT\n",
            "counted_review":binding}))
        path.chmod(0o600)
        return run

    def part(head, run):
        return {"pr":1, "head":head, "items":["demo:A"], "review_runs":{"demo:A":run}}

    def check_landing(world, head, run, allowed):
        row = part(head, run)
        batch = {"merged":[row], "worktree":tmp_path, "head":head}
        router = world.p("router")
        repo = "example/repo"
        lease = world.store.land(router, dict(repo=repo, action="acquire", owner="fixture"))
        prepare = dict(lease, repo=repo, pr=10, action="prepare",
                       base="a" * 40, head="b" * 40, parts=[row])
        if allowed:
            assert merge_batch_land.require_part_seats(batch) == {1:[run]}
            assert world.store.land(router, prepare)["parts"][0]["review_runs"] == {"demo:A":run}
        else:
            with pytest.raises(merge_batch_land.MergeBatchError, match="counted review"):
                merge_batch_land.require_part_seats(batch)
            with pytest.raises(Refused, match="no explicit ACCEPT"):
                world.store.land(router, prepare)

    assert [route(head) for head in heads] == [[new], [new], [new], [old]]
    assert [[row["id"] for row in mcp(head)["legs"]] for head in heads] == [[new], [new], [new], [old]]
    with pytest.raises(SpawnRefused, match="counted review requires leg " + old):
        mcp(heads[-1], leg=new)
    assert route(heads[-1]) == [old]

    from plangraph.keeper import config
    monkeypatch.setattr(config, "load_config", lambda _path: SimpleNamespace(spawner_records_dir=tmp_path / "mirror"))
    monkeypatch.setattr(merge_batch_land, "git", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(merge_batch_land, "part_items", lambda _st, rec: set(rec["items"]))
    (tmp_path / "keeper").mkdir()
    world = World(tmp_path / "keeper", backend="sqlite")
    world.store.ctx = {"spawner_records": SpawnerRecordAdapter(tmp_path / "mirror")}
    try:
        old_fourth = mirror(receipt(heads[3], 3, False, "run-old4"))
        new_fourth = mirror(receipt(heads[3], 3, True, "run-new4"))
        for index, head in enumerate(heads[:3]):
            run = mirror(receipt(head, index, True, f"run-new{index + 1}"))
            check_landing(world, head, run, True)
        check_landing(world, heads[3], old_fourth, True)
        check_landing(world, heads[3], new_fourth, False)

        counted = policy["counted_review"]
        samples = []
        for is_candidate in (False, True):
            for i in range(10):
                head = f"{10 + i + 10 * is_candidate:040x}"
                index = i % 3 if is_candidate else 3
                counted["heads"][head] = lanes[index]
                samples.append(receipt(head, index, is_candidate, f"run-sample{is_candidate}{i}",
                                       accepted=not is_candidate or i == 0))
        counted["decision"] = {"action":"promote", "revision":"new", "samples":samples}
        path.write_text(json.dumps(policy))
        assert route(heads[3]) == [old]
        check_landing(world, heads[3], new_fourth, False)

        for sample in samples:
            sample.update(accepted=True, round=1)
        path.write_text(json.dumps(policy))
        assert route(heads[3]) == [new]
        assert [row["id"] for row in mcp(heads[3])["legs"]] == [new]
        check_landing(world, heads[3], new_fourth, True)
        check_landing(world, heads[3], old_fourth, False)
        counted["decision"] = {"action":"rollback", "revision":"new"}
        path.write_text(json.dumps(policy))
        assert route(heads[3]) == [old]
        check_landing(world, heads[3], old_fourth, True)
        check_landing(world, heads[3], new_fourth, False)
    finally:
        world.close()


def test_remote_spawn_refuses_a_stale_counted_revision(tmp_path, monkeypatch, capsys):
    import json
    from plangraph.spawner.cli import main

    base = json.loads(Path("src/plangraph/planner/route_policy.json").read_text())
    path = tmp_path / "route.json"
    path.write_text(json.dumps(base))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    args = ["--state", str(tmp_path / "state"), "spawn", "--role", "review",
            "--card", '{"estimate_tokens":1000}', "--counted-policy-revision", "candidate",
            "--dry-run", "--json", "fixture"]
    assert main(args) != 0
    assert "receiving host differs" in capsys.readouterr().err
