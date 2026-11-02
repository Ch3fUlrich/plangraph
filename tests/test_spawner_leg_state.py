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
            return [dict(id=leg, available=1, active=True, aa_intelligence=50, cost_per_task_usd=cost, context_window=1000000) for leg, cost in [("cx/gpt-6.1-sol", 0), ("other/sol", 1)]]
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


@pytest.mark.parametrize("headers,body,reset", [
    ({"x-ratelimit-reset": "160"}, "", 160),
    ({}, "quota resets in 2 minutes", 220),
    ({"Retry-After": "60"}, "", 160),
])
def test_reset_formats(store, headers, body, reset):
    store.record_provider_response("cx/sol", 429, body, headers, "gateway", now=100)
    assert store.leg_status("cx/sol", now=101)["evidence"]["reset_at"] == reset


@pytest.mark.parametrize("body, delta", [
    ("(reset after 3h 12m 5s)", 11525),
    ("(reset after 45s)", 45),
    ("reset after 0s", None),
    ("resets in 2 minutes", 120),
    ("(reset after 4d 2h)", ls.RESET_CAP_S),
])
def test_reset_after_formats(store, body, delta):
    """D-3685: OmniRoute's "reset after" books like "resets in"; an elapsed
    cooldown ("reset after 0s") books nothing and a far-future reset is capped
    at now + 24 h."""
    store.record_provider_response("cx/zz-model", 429, body, {}, "gateway", now=100)
    assert store.leg_status("cx/zz-model", now=101)["evidence"]["reset_at"] == (
        None if delta is None else 100 + delta)


def test_probe_failure_does_not_hide_active_limit(store):
    """D-3685: a liveness-probe failure newer than a booked limit must not hide
    it while the limit's reset is still ahead."""
    store.record_harness_limit("agy/zz-lim", "error: quota reached. Resets in 10h",
                               "spawner:harness", now=1000)
    reset = 1000 + 36000
    store.record_probe_fail("agy/zz-lim", "timeout", "leg-probe", now=1100, down_s=60)
    state = store.leg_status("agy/zz-lim", now=1101)
    assert state["state"] == "limited"
    assert state["evidence"]["body_excerpt"] == "error: quota reached. Resets in 10h"
    assert state["evidence"]["reset_at"] == reset
    # Past the reset the probe-fail streak decides again: reprobe, then ok.
    assert store.leg_status("agy/zz-lim", now=reset + 1)["state"] == "reprobe"
    store.record_probe_pass("agy/zz-lim", "leg-probe", now=reset + 2)
    assert store.leg_status("agy/zz-lim", now=reset + 3)["state"] == "ok"
    # A newer success still lifts a limit early.
    store.record_harness_limit("agy/zz-lift", "error: quota reached. Resets in 10h",
                               "spawner:harness", now=1000)
    store.record_probe_pass("agy/zz-lift", "leg-probe", now=1100)
    assert store.leg_status("agy/zz-lift", now=1101)["state"] == "ok"


def test_runner_response_not_agent_claim(store):
    from plangraph.spawner.runner import SpawnerRunner, JobInfo
    runner = SpawnerRunner()
    runner.jobs["test"] = JobInfo("test", "failed", "429 quota", "cx/sol", stdout="429 quota")
    assert store.leg_status("cx/sol")["state"] == "ok"
    runner._adapter_tokens['test'] = object()
    runner.provider_response("test", "cx/sol", 429, "quota", {}, store=store,
                             source='gateway_adapter', adapter_token=runner._adapter_tokens['test'])
    assert store.leg_status("cx/sol")["state"] == "limited"
    runner.provider_response("test", "cx/sol", 200, "success", {}, store=store,
                             source='gateway_adapter', adapter_token=runner._adapter_tokens['test'])
    assert store.leg_status("cx/sol")["state"] == "ok"
