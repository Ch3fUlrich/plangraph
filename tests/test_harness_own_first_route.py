"""HARNESS-OWN-FIRST-ROUTE: every model runs in its own harness; OpenCode only when the client is missing."""
from dataclasses import replace

import pytest

from plangraph.planner.legs import (OWN_HARNESS, harness_skips, own_harness_legs, refuse_foreign_pin, resolve_harness,
                                    spawnable_legs)
from plangraph.routing_support.leg_state_flag import SpawnRefused
from registry_route_util import _repo

CATALOG = {leg.id: leg for leg in spawnable_legs()}
ALL = lambda name: True
NONE = lambda name: False


def test_own_harness_table():
    assert OWN_HARNESS == {"gpt": ("codex",), "gemini": ("agy",), "claude": ("claude", "agy")}


@pytest.mark.parametrize("leg_id,harness", [
    ("cx-gpt-6.2-sol-medium", "codex"), ("agy-gemini-4-hydrogen-high", "agy"),
    ("agy-claude-sonnet-5-5-high", "agy"), ("claude-sonnet-5-5-high", "claude")])
def test_installed_client_names_own_harness(leg_id, harness):
    choice = resolve_harness(CATALOG[leg_id], ALL)
    assert choice.harness == harness and choice.fallback is None


def test_gpt_opencode_leg_moves_to_codex_keeping_its_registered_model():
    from plangraph.model_admission import validate_model
    legs = own_harness_legs([CATALOG[i] for i in ("cx-gpt-6.2-sol-medium", "cx-gpt-6.2-astra", "cx-gpt-6.2-sol")], ALL)
    assert [(leg.harness, leg.harness_fallback) for leg in legs] == [("codex", None)] * 3
    for leg in legs:   # launch admission still knows the model
        assert validate_model(leg.model) == leg.model


def test_codex_client_takes_plain_name_and_effort():
    from plangraph.routing_support.clients import codex_model
    assert codex_model("omniroute/cx-gpt-6.2-sol-medium") == ("gpt-6.2-sol", "medium")
    assert codex_model("gpt-6.2-sol-medium") == ("gpt-6.2-sol", "medium")
    assert codex_model("omniroute/cx-gpt-6.2-astra") == ("gpt-6.2-astra", None)


def test_missing_client_keeps_the_opencode_leg_and_records_why():
    (leg,) = own_harness_legs((CATALOG["cx-gpt-6.2-sol-medium"],), NONE)
    assert (leg.harness, leg.model, leg.harness_fallback) == (
        "opencode", "omniroute/cx-gpt-6.2-sol-medium", "client_missing")


def test_agy_claude_leg_is_an_own_harness_no_fallback():
    """Operator 2026-11-20 21:08Z: Claude in agy counts as its own harness."""
    (leg,) = own_harness_legs((CATALOG["agy-claude-sonnet-5-5-high"],), ALL)
    assert leg.harness == "agy" and leg.harness_fallback is None
    assert resolve_harness(CATALOG["agy-claude-sonnet-5-5-high"], ALL).fallback is None


def test_families_without_own_client_stay_on_opencode():
    leg = CATALOG["opencode-deepseek-v4.1-flash-free"]
    assert own_harness_legs((leg,), NONE)[0] == leg and own_harness_legs((leg,), ALL)[0] == leg


def test_foreign_pin_needs_an_override_reason_while_the_own_client_is_installed():
    gpt = CATALOG["cx-gpt-6.2-astra"]
    with pytest.raises(SpawnRefused, match="override"):
        refuse_foreign_pin(gpt, None, ALL)
    with pytest.raises(SpawnRefused, match="override"):
        refuse_foreign_pin(gpt, "short", ALL)
    refuse_foreign_pin(gpt, "measured: codex sandbox fails on this host", ALL)
    refuse_foreign_pin(gpt, None, NONE)  # the OpenCode leg is the fallback there
    refuse_foreign_pin(replace(gpt, harness="codex"), None, ALL)
    refuse_foreign_pin(CATALOG["agy-claude-sonnet-5-5-high"], None, ALL)
    refuse_foreign_pin(CATALOG["opencode-deepseek-v4.1-flash-free"], None, ALL)


def routed(tmp_path, installed, role="implement", writers=()):
    from types import SimpleNamespace

    from plangraph.spawner.service import SpawnService
    service = SpawnService(SimpleNamespace(default_cwd=str(tmp_path), jobs={}, _tasks={}, _processes={}),
                           state=tmp_path / "state")
    service.client_probe = installed
    return service.route({"estimate_tokens": 1000}, role, writers)


def test_service_route_names_codex_for_gpt_and_falls_back_to_opencode(tmp_path):
    plan = routed(tmp_path, ALL)
    assert plan.legs[0].id == "cx-gpt-6.2-sol-medium" and plan.legs[0].harness == "codex"
    plan = routed(tmp_path, lambda name: name != "codex")
    assert (plan.legs[0].harness, plan.legs[0].harness_fallback) == ("opencode", "client_missing")
    assert plan.to_dict()["first_leg"]["harness_fallback"] == "client_missing"


@pytest.fixture
def review_diff_ref(tmp_path):
    repo, base, head = _repo(tmp_path)
    return {"repo": str(repo), "base": base, "head": head}


def test_service_route_review_for_claude_diff_runs_gpt_in_codex(tmp_path, review_diff_ref):
    plan = review_route(tmp_path, ALL, review_diff_ref)
    assert [(leg.id, leg.harness) for leg in plan.legs[:2]] == [
        ("agy-gemini-4-hydrogen-high", "agy"), ("gpt-6.2-sol-medium", "codex")]


def test_service_spawn_refuses_a_foreign_pin_without_a_reason(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from plangraph.spawner.service import SpawnService
    service = SpawnService(SimpleNamespace(default_cwd=str(tmp_path), jobs={}, _tasks={}, _processes={}),
                           state=tmp_path / "state")
    service.client_probe = ALL
    with pytest.raises(SpawnRefused, match="foreign-harness pin refused"):
        asyncio.run(service.spawn(card={"estimate_tokens": 1000}, prompt="x", role="implement",
                                  leg="cx-gpt-6.2-sol", dry_run=True))


def test_inline_only_leg_keeps_its_opencode_harness():
    leg = CATALOG["cx-gpt-6-luna"]
    assert leg.tool_mode == "inline-only" and own_harness_legs((leg,), ALL)[0] == leg


def test_switch_off_keeps_registered_harness(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from plangraph.spawner.service import SpawnService
    service = SpawnService(SimpleNamespace(default_cwd=str(tmp_path), jobs={}, _tasks={}, _processes={}),
                           state=tmp_path / "state")
    from plangraph.planner.legs import service_probe
    assert service_probe(service) is None
    monkeypatch.delenv("PLANGRAPH_OWN_HARNESS")
    from plangraph.planner.legs import client_installed, service_probe
    assert service_probe(service) is client_installed
    assert resolve_harness(CATALOG["cx-gpt-6.2-sol-medium"], None).harness == "opencode"


def test_native_legs_with_a_missing_client_fall_back_or_leave_the_route():
    review = CATALOG["gpt-6.2-sol-medium"]
    (leg,) = own_harness_legs((review,), NONE)   # gpt: the OpenCode leg of the same model
    assert (leg.harness, leg.model, leg.harness_fallback, leg.tool_mode) == (
        "opencode", "omniroute/cx-gpt-6.2-sol-medium", "client_missing", "inline-only")
    for leg_id in ("agy-gemini-4-hydrogen-high", "agy-claude-sonnet-5-5-high", "claude-sonnet-5-5-high"):
        assert own_harness_legs((CATALOG[leg_id],), NONE) == ()   # no OpenCode counterpart serves them
    assert [row["leg"] for row in harness_skips((review, CATALOG["claude-sonnet-5-5-high"]), NONE)] == [
        "claude-sonnet-5-5-high"]


def test_probe_failure_across_the_three_families_through_the_route(tmp_path, review_diff_ref):
    plan = routed(tmp_path, NONE)
    assert (plan.legs[0].harness, plan.legs[0].harness_fallback) == ("opencode", "client_missing")
    assert "agy-gemini-4-hydrogen-high" not in [leg.id for leg in plan.legs]
    assert any(row["leg"] == "agy-gemini-4-hydrogen-high" and row["reason"] == "client_missing" for row in plan.skipped)
    review = review_route(tmp_path, NONE, review_diff_ref)
    assert review.legs[0].id == "gpt-6.2-sol-medium"
    assert (review.legs[0].harness, review.legs[0].harness_fallback) == ("opencode", "client_missing")
    ids = [leg.id for leg in review.legs]
    assert "claude-sonnet-5-5-high" not in ids
    assert any(row["leg"] == "claude-sonnet-5-5-high" and row["reason"] == "client_missing" for row in review.skipped)
    gemini = review_route(tmp_path, NONE, review_diff_ref, ("gpt",))
    assert all(leg.family != "gemini" or leg.harness == "agy" for leg in gemini.legs)


def review_route(tmp_path, installed, diff_ref, writers=("claude",)):
    from types import SimpleNamespace

    from plangraph.spawner.service import SpawnService
    service = SpawnService(SimpleNamespace(default_cwd=str(tmp_path), jobs={}, _tasks={}, _processes={}),
                           state=tmp_path / "state")
    service.client_probe = installed
    card = {"estimate_tokens": 1000, "diff_ref": diff_ref}
    return service.route(card, "review", writers)


def test_paused_codex_harness_removes_the_converted_leg(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text('{"paused_harnesses": {"codex": {"allowed_roles": []}}}')
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(policy))
    plan = routed(tmp_path, ALL)
    assert all(leg.harness != "codex" for leg in plan.legs)
    assert any(row["reason"] == "paused_harness" and row["leg"] == "cx-gpt-6.2-sol-medium" for row in plan.skipped)


def codex_limited(model, **kwargs):
    limited = model.startswith("omniroute/cx-gpt-") or model in {
        "gpt-6.2-sol-medium", "gemini-4-hydrogen-high"}
    return {"state": "limited" if limited else "available", "evidence": {"status": 429} if limited else None}


def test_agy_claude_fallback_writer_needs_its_client_when_codex_is_limited():
    from plangraph.planner.routing import plan_spawn
    card = {"estimate_tokens": 20000, "risk_tags": []}
    served = plan_spawn(card, role="implement", leg_reader=codex_limited, installed=ALL)
    assert served.first_leg.id == "agy-claude-sonnet-5-5-high" and served.first_leg.harness == "agy"
    missing = plan_spawn(card, role="implement", leg_reader=codex_limited, installed=NONE)
    assert "agy-claude-sonnet-5-5-high" not in [leg.id for leg in missing.legs]
    assert any(row["leg"] == "agy-claude-sonnet-5-5-high" and row["reason"] == "client_missing"
               for row in missing.skipped)


def test_each_leg_is_probed_once_so_legs_and_skips_never_overlap():
    calls = []

    def flaky(name):
        calls.append(name)
        return len(calls) % 2 == 0   # a probe whose answer changes between calls
    from plangraph.planner.legs import resolve_legs
    legs, skips = resolve_legs([CATALOG["agy-gemini-4-hydrogen-high"], CATALOG["agy-gemini-4-hydrogen-medium"]], flaky)
    assert len(calls) == 2
    assert not {leg.id for leg in legs} & {row["leg"] for row in skips}
