"""D-4147: OmniRoute alone owns expiry; old config rows are ignored."""
import ast
import dataclasses
import json
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, leg_active, load_legs
from plangraph.planner.routing import plan_spawn


def test_d3605_sol_routes_implement_never_review():
    card = {"estimate_tokens": 20000, "risk_tags": []}
    assert plan_spawn(card, role="implement").first_leg.id == "cx-gpt-6.1-sol"
    review = plan_spawn(card, role="review", writer_families=("mimo",))
    assert "cx-gpt-6.1-sol" not in [leg.id for leg in review.legs]


@pytest.mark.parametrize("expires", ["2000-01-01", "invalid", None, 123])
def test_load_legs_ignores_legacy_expires(tmp_path, expires):
    raw = dataclasses.asdict(next(l for l in DEFAULT_LEGS if l.id == "cx-gpt-6.1-sol"))
    raw["expires"] = expires
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [raw]}))
    leg, = load_legs(path)
    assert not hasattr(leg, "expires")
    assert leg_active(leg)
    assert plan_spawn({"estimate_tokens": 1000}, legs=(leg,), role="implement").first_leg == leg


def test_disabled_leg_stays_inactive():
    assert not leg_active(dataclasses.replace(DEFAULT_LEGS[0], disabled="policy"))


@pytest.mark.parametrize("state", ["expired", "down"])
def test_omniroute_expired_or_down_no_route_or_override(tmp_path, state):
    import asyncio
    from plangraph.planner.routing import SpawnRefused
    from plangraph.spawner.service import SpawnService
    from plangraph.spawner.runner import SpawnerRunner

    leg = next(l for l in DEFAULT_LEGS if l.id == "cx-gpt-6.1-sol")
    def reader(model, **kwargs):
        return {"state": state if model == leg.model else "ok", "evidence": None}
    card = {"estimate_tokens": 1000}
    plan = plan_spawn(card, role="implement", leg_reader=reader)
    assert leg.id not in {candidate.id for candidate in plan.legs}
    for selection in ({"leg": leg.id}, {"model": leg.model}):
        with pytest.raises(SpawnRefused, match=state):
            asyncio.run(SpawnService(runner=SpawnerRunner(), state=tmp_path, leg_reader=reader).spawn(
                card=card, prompt="task", role="implement", override_reason="D-4151 hermetic regression",
                dry_run=True, **selection))
    assert not (tmp_path / "spawn-overrides.jsonl").exists()


def local_state_definitions(source):
    """CI guard: reject local state fields/keywords/dict literals, not disabled policy.

    Only the exact legacy-row comparison and removal may mention expiry.
    """
    forbidden = {"expires", "available", "availability", "active", "unavailable_until"}
    hits = []
    tree = ast.parse(source)
    compatibility = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare)
                and ast.unparse(node) == "key != 'expires'"):
            compatibility.update(node.comparators)
        if (isinstance(node, ast.Call)
                and ast.unparse(node) == "data.pop('expires', None)"):
            compatibility.add(node.args[0])
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value in forbidden and node not in compatibility):
            hits.append(node.lineno)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in forbidden:
                hits.append(node.lineno)
        if isinstance(node, ast.Call):
            hits.extend(k.value.lineno for k in node.keywords if k.arg in forbidden)
        if isinstance(node, ast.Dict):
            hits.extend(k.lineno for k in node.keys
                        if isinstance(k, ast.Constant) and k.value in forbidden)
    return hits


def test_no_local_state_in_leg_definitions():
    from plangraph.planner import legs
    assert not local_state_definitions(Path(legs.__file__).read_text())


@pytest.mark.parametrize("literal", ["expires='2000-01-01'", "available=True",
                                    "availability='up'", "active=True"])
def test_local_state_guard_red_case(literal):
    assert local_state_definitions(f"Leg('injected', {literal})")
    key, value = literal.split("=")
    assert local_state_definitions(f"row = {{{key!r}: {value}}}")
    assert local_state_definitions(f"class Leg:\n    {key}: str = {value}")
    assert not local_state_definitions("Leg('retained', disabled='policy')")


@pytest.mark.parametrize("source", ['_STR_FIELDS = ("id", "expires")',
                                    'state_field = "available"'])
def test_local_state_guard_string_constants(source):
    assert local_state_definitions(source)


def test_config_row_schema_has_no_local_state(tmp_path):
    from plangraph.planner import legs
    forbidden = {"expires", "available", "availability", "active", "unavailable_until"}
    for fields in (legs._FIELDS, legs._REQUIRED, legs._STR_FIELDS):
        assert not forbidden.intersection(fields)
    raw = dataclasses.asdict(DEFAULT_LEGS[0])
    for key in forbidden - {"expires"}:
        path = tmp_path / "legs.json"
        path.write_text(json.dumps({"legs": [dict(raw, **{key: "up"})]}))
        with pytest.raises(ValueError, match="unknown field"):
            load_legs(path)
