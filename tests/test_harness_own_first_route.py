"""HARNESS-OWN-FIRST-ROUTE: every model runs in its own harness; OpenCode only when the client is missing."""
from dataclasses import replace

import pytest

from plangraph.planner.legs import (OWN_HARNESS, own_harness_legs, refuse_foreign_pin, resolve_harness,
                                    spawnable_legs)
from plangraph.routing_support.leg_state_flag import SpawnRefused

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


def test_gpt_opencode_leg_moves_to_codex_with_the_codex_model_name():
    (leg,) = own_harness_legs((CATALOG["cx-gpt-6.2-sol-medium"],), ALL)
    assert (leg.harness, leg.model, leg.harness_fallback) == ("codex", "gpt-6.2-sol-medium", None)


def test_missing_client_keeps_the_opencode_leg_and_records_why():
    (leg,) = own_harness_legs((CATALOG["cx-gpt-6.2-sol-medium"],), NONE)
    assert (leg.harness, leg.model, leg.harness_fallback) == (
        "opencode", "omniroute/cx-gpt-6.2-sol-medium", "client_missing")


def test_agy_claude_leg_is_an_own_harness_no_fallback():
    """Operator 2026-11-20 21:08Z: Claude in agy counts as its own harness."""
    for installed in (ALL, NONE):
        (leg,) = own_harness_legs((CATALOG["agy-claude-sonnet-5-5-high"],), installed)
        assert leg.harness == "agy" and leg.harness_fallback is None


def test_families_without_own_client_stay_on_opencode():
    leg = CATALOG["opencode-deepseek-v4.1-flash-free"]
    assert own_harness_legs((leg,), NONE)[0] == leg and own_harness_legs((leg,), ALL)[0] == leg


def test_foreign_pin_needs_an_override_reason_while_the_own_client_is_installed():
    gpt = CATALOG["cx-gpt-6.2-sol-medium"]
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


def test_service_route_review_for_claude_diff_runs_gpt_in_codex(tmp_path):
    from pathlib import Path
    service_card = {"estimate_tokens": 1000, "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
    from types import SimpleNamespace

    from plangraph.spawner.service import SpawnService
    service = SpawnService(SimpleNamespace(default_cwd=str(tmp_path), jobs={}, _tasks={}, _processes={}),
                           state=tmp_path / "state")
    service.client_probe = ALL
    plan = service.route(service_card, "review", ("claude",))
    assert (plan.legs[0].id, plan.legs[0].harness) == ("gpt-6.2-sol-medium", "codex")


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
