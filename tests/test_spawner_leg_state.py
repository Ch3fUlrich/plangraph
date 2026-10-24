from functools import partial

import pytest

from plangraph.spawner import leg_state as ls


@pytest.fixture
def store(tmp_path):
    return ls.LegStateStore(tmp_path / "legs.sqlite")


@pytest.mark.parametrize("status", [429, 402])
def test_reset(store, status):
    store.record_provider_response("cx/gpt-6.1-sol", status, "quota", {"Retry-After": "60"}, "gateway", now=100)
    result = store.leg_status("cx/gpt-6.1-sol", now=159)
    assert result["state"] == "limited"
    assert result["evidence"]["status"] == status
    assert result["evidence"]["reset_at"] == 160
    assert store.leg_status("cx/gpt-6.1-sol", now=160)["state"] == "ok"


def test_reprobe(store):
    store.record_provider_response("cx/sol", 429, "quota", {}, "runner", now=100)
    assert store.leg_status("cx/sol", now=699)["state"] == "limited"
    assert store.leg_status("cx/sol", now=700)["state"] == "reprobe"
    store.record_probe_pass("cx/sol", now=701, caller="gateway")
    assert store.leg_status("cx/sol", now=702)["state"] == "ok"


@pytest.mark.parametrize("status,body,caller", [(200, "quota", "runner"), (500, "quota", "runner"), (429, "", "runner"), (429, "quota", "")])
def test_refuse(store, status, body, caller):
    with pytest.raises(ls.LegStateError):
        store.record_provider_response("cx/sol", status, body, {}, caller)
    assert not hasattr(ls, "mark_limited")
    assert not hasattr(ls, "set_limited")


def test_dimensions_and_redaction(store):
    store.record_provider_response("cx/sol", 429, "sk-example-secret Bearer example-token", {}, "runner", now=100, account="A", combo="one")
    state = store.leg_status("cx/sol", now=101, account="A", combo="one")
    assert state["state"] == "limited"
    assert "sk-" not in state["evidence"]["body_excerpt"]
    assert "example-token" not in state["evidence"]["body_excerpt"]
    for leg, account, combo in [("cx/sol", "B", "one"), ("cx/sol", "A", "two"), ("other/sol", "A", "one")]:
        assert store.leg_status(leg, now=101, account=account, combo=combo)["state"] == "ok"


def test_route(store):
    from plangraph.spawner.resolver import resolve_model_for_task
    class Catalog:
        def list_models(self, **kwargs):
            return [dict(id=leg, aa_intelligence=50, cost_per_task_usd=cost, context_window=1000000) for leg, cost in [("cx/gpt-6.1-sol", 0), ("other/sol", 1)]]
    store.record_provider_response("cx/gpt-6.1-sol", 429, "quota", {"Retry-After": "60"}, "gateway", now=100, combo="l2-worker")
    def route(now):
        return resolve_model_for_task(db=Catalog(), leg_state_reader=partial(store.leg_status, now=now))
    result = route(101)
    assert result["model"] == "other/sol"
    assert result["leg_state"]["state"] == "ok"
    assert result["candidates"][0]["leg_state"]["evidence"]["status"] == 429
    assert route(160)["model"] == "cx/gpt-6.1-sol"


def test_no_mcp_writer():
    from plangraph.spawner.mcp import TOOLS_METADATA
    assert not any("leg_state" in str(tool) or "leg-state" in str(tool) for tool in TOOLS_METADATA)


def test_view():
    from plangraph.views import leg_state
    assert leg_state([{"id": "cx/sol", "leg_state": {"state": "reprobe", "evidence": None}}]) == [{"id": "cx/sol", "leg_state": {"state": "reprobe", "evidence": None}}]
