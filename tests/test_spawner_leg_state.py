from functools import partial

import pytest

from plangraph.spawner import leg_state as ls


@pytest.mark.parametrize('leg_id', ['agy-gemini-4-argon', 'opencode-big-pickle',
                                  'opencode-deepseek-v4.1-flash-free'])
def test_missing_legs_roles_and_probe_evidence(leg_id, store):
    from plangraph.planner.legs import DEFAULT_LEGS, leg_active
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)
    assert leg_active(leg)
    assert set(leg.roles) == {'review', 'implement'}
    store.record_probe_pass(leg.model, 'leg-probe', now=100)
    assert store.last_probe_ts(leg.model) == 100
    assert store.leg_status(leg.model, now=101)['state'] == 'ok'


def test_go_usage_limit_leaves_personal_free_available(store):
    from plangraph.routing_support.resolver import is_model_available
    go, personal = 'opencode-go/gemini-4-argon', 'opencode/big-pickle'
    store.record_provider_response(go, 429, 'usage_limit', {'Retry-After': '60'}, 'test')
    store.record_probe_pass(personal, 'leg-probe')
    rows = [{'id': model, 'tier': tier, 'available': 1, 'active': True} for model, tier in
            [(go, 'subscription'), (personal, 'free')]]
    available = [row['id'] for row in rows if is_model_available(row, store.leg_status)]
    assert available == [personal]


def test_route_free_first_keys():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS, TIERS
    from plangraph.planner.review_order import review_key
    from plangraph.planner.route_policy import implement_key
    sol = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6.1-sol')
    free = replace(sol, id='free-test', tier='free', family='gemini')
    for key in (review_key, implement_key):
        assert key(free, TIERS.index('free'), 1) < key(sol, TIERS.index('subscription'), 0)


def test_catalog_tiers_preserve_roles():
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.review_order import catalog_tiers
    rows = [{'id': 'gemini-3.8-flash', 'tier': 'free'}]
    updated = catalog_tiers(DEFAULT_LEGS, rows)
    assert next(l for l in updated if l.id == 'agy-gemini-3.8-flash').tier == 'free'
    assert [l.roles for l in updated] == [l.roles for l in DEFAULT_LEGS]
    assert next(l for l in updated if l.id == 'deepseek').tier == 'paid'


def test_manual_disable_expiry():
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS, leg_active
    leg = replace(DEFAULT_LEGS[1], disabled='temporary operator stop', expires=160)
    assert not leg_active(leg, now=159)
    assert leg_active(leg, now=160)
    assert not leg_active(replace(leg, expires=None), now=100000)


def test_old_limit_evidence_reprobes(store):
    store.record_provider_response('test/leg', 429, 'quota', {'Retry-After': '86400'}, 'test', now=100)
    assert store.leg_status('test/leg', now=100 + 21600)['state'] == 'reprobe'
    # Also exercise a legacy row whose recorded reset exceeds the new cap.
    with store._connect() as db:
        db.execute('UPDATE leg_events SET reset_at=?', (100 + 86400,))
    assert store.leg_status('test/leg', now=100 + 21600)['state'] == 'reprobe'


@pytest.mark.parametrize('age,reset', [(21600, 86400), (60, 60)])
def test_expired_probe_failure_cannot_complete_down_streak(store, age, reset):
    store.record_probe_fail('test/expiry', 'timeout', 'test', now=100, down_s=reset)
    store.record_probe_fail('test/expiry', 'timeout', 'test', now=100 + age, down_s=86400)
    assert store.leg_status('test/expiry', now=100 + age)['state'] == 'reprobe'
    store.record_probe_fail('test/expiry', 'timeout', 'test', now=101 + age, down_s=86400)
    assert store.leg_status('test/expiry', now=101 + age)['state'] == 'down'


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
    ("(reset after 500ms)", 0.5),
    ("reset after 0s", None),
    ("resets in 2 minutes", 120),
    ("(reset after 4d 2h)", ls.RESET_CAP_S),
])
def test_reset_after_formats(store, body, delta):
    """D-3685: OmniRoute's "reset after" books like "resets in"; an elapsed
    cooldown ("reset after 0s") books nothing at all and a far-future reset is
    capped at now + 24 h."""
    store.record_provider_response("cx/zz-model", 429, body, {}, "gateway", now=100)
    state = store.leg_status("cx/zz-model", now=101)
    if delta is None:
        assert state["state"] == "ok"  # no reset-less limit row was written
    else:
        assert state["evidence"]["reset_at"] == 100 + delta


@pytest.mark.parametrize("text, seconds", [
    ("reset after 500ms", 0.5),
    ("reset after 3h 12m 5s", 11525),
    ("resets in 2 minutes", 120),
    ("reset after 1d", 86400),
    ("reset after 5 parsecs", None),
])
def test_reset_seconds_unit_parsing(text, seconds):
    """D-3685: units match longest-first with a word boundary -- "500ms" is
    0.5 s, never 500 minutes -- and an unknown unit makes the whole reset
    unparsed (None), never a misread."""
    assert ls._reset_seconds(text) == seconds


def test_explicit_zero_cooldown_books_nothing(store):
    """D-3685: one shared rule for an explicitly elapsed cooldown ("reset
    after 0s" prose, a retry-after header of 0): no booking path writes a
    limit row or a down_s fallback for it."""
    store.record_provider_response("cx/zero", 429, "quota reached (reset after 0s)", {}, "gw", now=1000)
    assert store.leg_status("cx/zero", now=1001)["state"] == "ok"
    store.record_provider_response("cx/zero2", 429, "quota", {"retry-after": "0"}, "gw", now=1000)
    assert store.leg_status("cx/zero2", now=1001)["state"] == "ok"
    store.record_harness_limit("cx/zero3", "error: quota reached. Resets in 0s",
                               "test", now=1000, down_s=1800)
    assert store.leg_status("cx/zero3", now=1001)["state"] == "ok"
    assert not store.path.exists()  # nothing was booked at all


def test_probe_fail_down_window_is_capped(store, monkeypatch):
    """D-3685: "every reset is capped" holds on the probe-fail booking path
    too: a large down_s or PLANGRAPH_LEG_DOWN_S never parks a leg for days."""
    store.record_probe_fail("agy/zz-cap", "timeout", "leg-probe", now=1000, down_s=172800)
    store.record_probe_fail("agy/zz-cap", "timeout", "leg-probe", now=1001, down_s=172800)
    assert store.leg_status("agy/zz-cap", now=4600)["state"] == "down"
    assert store.leg_status("agy/zz-cap", now=1001)["evidence"]["reset_at"] == 1001 + ls.RESET_CAP_S
    assert store.leg_status("agy/zz-cap", now=1001 + ls.RESET_CAP_S + 1)["state"] != "down"
    monkeypatch.setenv("PLANGRAPH_LEG_DOWN_S", "172800")
    store.record_probe_fail("agy/zz-envcap", "timeout", "leg-probe", now=1000)
    assert store.leg_status("agy/zz-envcap", now=1001)["evidence"]["reset_at"] == 1000 + ls.RESET_CAP_S


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
