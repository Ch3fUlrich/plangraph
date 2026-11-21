from dataclasses import replace
from pathlib import Path

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.route_policy import load_route_policy
from plangraph.planner.routing import plan_spawn, route_card


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
    assert review.first_leg.id == "agy-gemini-4-hydrogen-high"
    assert all(not leg.claude_family for leg in (*writer.legs, *review.legs))
    assert sensitive.first_leg.id == "claude-sonnet-5-5-high"

    def limited(model, **kwargs):
        state = available(model)
        if model in {"gpt-6.2-sol-medium", "gemini-4-hydrogen-high"}:
            state["state"] = "limited"
        return state

    assert any(leg.claude_family for leg in plan("review", reader=limited).legs)
    assert any(leg.claude_family for leg in plan("implement", reader=limited).legs)


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
        ("review", "HEAD~2", "HEAD~1", ("agy-gemini-4-hydrogen-high", "agy")),
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
    assert plan.first_leg.id == "agy-gemini-4-hydrogen-high"
    assert all(leg.harness != "opencode" and leg.tool_mode == "tools" for leg in plan.legs)
    reasons = {row["leg"]: row["reason"] for row in plan.skipped}
    assert reasons["opencode-go-deepseek-v4-flash"] == "repository_review_tools_unavailable"


def test_native_review_cli_preview_reports_empty_route_with_causes(tmp_path, monkeypatch, capsys):
    import json

    from plangraph.planner import legs
    from plangraph.spawner.cli import main

    monkeypatch.delenv("PLANGRAPH_OWN_HARNESS", raising=False)
    monkeypatch.setattr(legs, "client_installed", lambda name: name == "opencode")
    card = {"estimate_tokens": 1000, "repository_review_required": True}
    assert main(["--state", str(tmp_path), "route", "--role", "review", "--writer-family", "gpt",
                 "--json", "--card", json.dumps(card)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["legs"] == []
    reasons = {row["leg"]: row["reason"] for row in plan["skipped"]}
    assert reasons["agy-gemini-4-hydrogen-high"] == "client_missing"
    assert reasons["opencode-go-deepseek-v4-flash"] == "repository_review_tools_unavailable"
