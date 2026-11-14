"""Operator JSON preserves validated leg fields through implement route records."""
import asyncio
import dataclasses
import json

import pytest

from plangraph.planner.legs import CX_EFFORTS, Leg, load_legs
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.service import SpawnService


@pytest.fixture(autouse=True)
def hermetic_spawn_boundaries(monkeypatch, tmp_path):
    async def admit(*args, **kwargs):
        return True
    async def prompt(self, text, *args, **kwargs):
        return text
    monkeypatch.setattr('plangraph.spawner.service.admit_base', admit)
    monkeypatch.setattr('plangraph.spawner.seat_spawn.admit', lambda *a, **k: None)
    monkeypatch.setattr('plangraph.spawner.service.SpawnService._annotate_spawn', lambda *a, **k: None)
    monkeypatch.setattr('plangraph.spawner.service.SpawnService._prepare_leg_prompt', prompt)
    monkeypatch.chdir(tmp_path)


def sol_row(**fields):
    row = dict(id="cx-gpt-6.1-sol-xhigh", tier="subscription", harness="opencode",
               model="omniroute/cx-gpt-6.1-sol-xhigh", family="gpt",
               windows=["128k", "256k", "1m"], max_request_tokens=900000,
               trains_on_data=False, roles=["implement"], effort="xhigh")
    return dict(row, **fields)


def load_row(tmp_path, row):
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [row]}), encoding="utf-8")
    leg, = load_legs(path)
    return leg


@pytest.mark.parametrize("effort", [*CX_EFFORTS, None])
def test_json_effort_reaches_implement_route_and_saved_record(tmp_path, monkeypatch, effort):
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(tmp_path / "absent.db"))
    def ambient_catalog():
        pytest.fail("JSON effort roundtrip must not read the host catalog")
    monkeypatch.setattr("plangraph.routing_support.resolver.get_catalog_models", ambient_catalog)
    model = "omniroute/cx-gpt-6.1-sol" + ("-" + effort if effort else "")
    leg = load_row(tmp_path, sol_row(id=model.split("/", 1)[1], model=model, effort=effort))
    assert leg.effort == effort
    card = {"estimate_tokens": 1000, "effort": effort, "effort_decision": "D-5840"}
    reader = lambda *a, **kw: {"state": "available", "evidence": None}
    plan = plan_spawn(card, role="implement", legs=(leg,), policy=RoutePolicy(),
                      leg_reader=reader, catalog_rows=())
    route = json.loads(json.dumps(plan.to_dict()))
    assert route["first_leg"]["effort"] == effort
    assert route["legs"][0]["effort"] == effort
    monkeypatch.setattr("plangraph.routing_support.resolver.get_catalog_models", lambda: [])
    service = SpawnService(state=tmp_path, leg_reader=reader)
    record = asyncio.run(service.spawn(card=card, prompt="Implement public code",
                                       dry_run=True, _plan=plan))
    assert record["status"] == "dry_run"
    assert record["chosen_leg"]["effort"] == effort
    assert record["plan"]["first_leg"]["effort"] == effort
    assert record["attempts"][0]["leg"]["effort"] == effort
    assert service.result(record["run_id"])["chosen_leg"]["effort"] == effort


def test_json_omitted_effort_defaults_to_none(tmp_path):
    row = sol_row()
    del row["effort"]
    assert load_row(tmp_path, row).effort is None


@pytest.mark.parametrize("effort", ["ultra", "", 1, True, [], {}])
def test_json_invalid_effort_is_refused(tmp_path, effort):
    with pytest.raises(ValueError, match=r"legs\[0\]\.effort"):
        load_row(tmp_path, sol_row(effort=effort))


@pytest.mark.parametrize("field", [field.name for field in dataclasses.fields(Leg)])
def test_every_json_leg_field_is_preserved(tmp_path, field):
    # Non-default optional values catch validated fields silently dropped at construction.
    row = sol_row(claude_family=True, note="operator JSON preservation regression",
                  roles=["review", "implement"], tool_mode="inline-only",
                  disabled="operator policy", trains_on_data=True, effort="high", expires=123)
    leg = load_row(tmp_path, row)
    assert set(row) == {field.name for field in dataclasses.fields(Leg)}
    expected = tuple(row[field]) if field in ("windows", "roles") else row[field]
    assert getattr(leg, field) == expected
