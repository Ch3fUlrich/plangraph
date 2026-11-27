from functools import partial
from dataclasses import replace

import pytest

from plangraph.spawner import leg_state as ls
from plangraph.planner.legs import DEFAULT_LEGS, TIERS, leg_active
from plangraph.planner.review_order import catalog_tiers, review_key
from plangraph.planner.routing import SpawnPlan, plan_spawn


@pytest.mark.parametrize('leg_id', ['agy-gemini-4-neon-medium', 'opencode-big-pickle',
                                  'opencode-deepseek-v4.1-flash-free',
                                  'opencode-glm-5.3-flash-free'])
def test_missing_legs_roles_and_probe_evidence(leg_id, store):
    leg = next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)
    if leg_id == 'opencode-glm-5.3-flash-free':
        assert not leg_active(leg)
        assert leg.disabled.startswith('D-6310:')
    elif leg_id == 'agy-gemini-4-neon-medium':
        assert not leg_active(leg)
        assert leg.disabled == 'D-6957 operator cost hold'
    else:
        assert leg_active(leg)
    expected = {'review'} if leg_id == 'agy-gemini-4-neon-medium' else {'review', 'implement'}
    assert set(leg.roles) == expected
    store.record_probe_pass(leg.model, 'leg-probe', now=100)
    assert store.last_probe_ts(leg.model) == 100
    assert store.leg_status(leg.model, now=101)['state'] == 'ok'


def test_go_usage_limit_leaves_personal_free_available(store):
    from plangraph.routing_support.resolver import is_model_available
    go, personal = 'opencode-go/glm-5.3-flash', 'opencode/big-pickle'
    store.record_provider_response(go, 429, 'usage_limit', {'Retry-After': '60'}, 'test')
    store.record_probe_pass(personal, 'leg-probe')
    rows = [{'id': model, 'tier': tier, 'available': 1, 'active': True} for model, tier in
            [(go, 'subscription'), (personal, 'free')]]
    available = [row['id'] for row in rows if is_model_available(row, store.leg_status)]
    assert available == [personal]


def test_provider_exhaustion_keeps_static_policy_refusals_separate():
    from plangraph.routing_support.liveness import provider_exhausted
    policy = [{'reason': 'off_family'}, {'reason': 'disabled: operator stop'}]
    assert not provider_exhausted(policy)
    for state in ('limited', 'down', 'expired'):
        assert provider_exhausted([*policy, {'reason': state}])
        assert not provider_exhausted([{'reason': state}, {'reason': 'window_class'}])


@pytest.mark.parametrize('reason', ('not_listed', 'provider_inactive', 'unreachable'))
def test_provider_exhaustion_counts_limits_on_the_eligible_legs(reason):
    from plangraph.routing_support.liveness import provider_exhausted
    unavailable = {'reason': reason}
    assert not provider_exhausted([unavailable])
    assert provider_exhausted([unavailable, {'reason': 'limited'}])


@pytest.mark.parametrize(('line', 'reason'), [
    ('model_unavailable: model is unavailable', 'model_unavailable'),
    ('HTTP 503: temporarily unavailable', 'provider_busy'),
    ('Error: overloaded', 'provider_busy'),
])
def test_provider_exhaustion_counts_temporary_harness_outages(store, line, reason):
    from plangraph.routing_support.liveness import provider_exhausted
    model = 'opencode-go/glm-5.3-flash'
    store.record_harness_limit(model, line, 'spawner:harness', now=100)
    status = ls.routing_status(store.leg_status(model, now=101))
    assert status['reason'] == reason
    outage = {'reason': status['reason']}
    assert provider_exhausted([outage, {'reason': 'not_listed'}])
    assert provider_exhausted([{'reason': 'limited'}, outage])
    assert not provider_exhausted([outage, {'reason': 'auth_refused'}])


@pytest.mark.parametrize('role', ('implement', 'review', 'orchestrate'))
@pytest.mark.parametrize('state', ('limited', 'down'))
def test_role_automatically_falls_back(role, state, monkeypatch):
    monkeypatch.setattr('plangraph.routing_support.resolver.get_catalog_models', lambda: [])
    card = {'estimate_tokens': 1000}
    kwargs = dict(role=role, writer_families=('claude',))
    clear = lambda *a, **k: {'state': 'ok', 'evidence': None}
    first = plan_spawn(card, leg_reader=clear, catalog_rows=(), **kwargs)
    assert len(first.legs) >= 2
    blocked = first.legs[0].model
    def reader(model, **kwargs):
        return {'state': state if model == blocked else 'ok', 'evidence': None}
    fallback = plan_spawn(card, leg_reader=reader, catalog_rows=(), **kwargs)
    if role == 'implement' and state == 'limited':   # D-6652: Sonnet high serves while the Codex lead is limited
        assert fallback.legs[0].id == 'claude-sonnet-5-5-high' and fallback.legs[1] == first.legs[1]
    else:
        assert fallback.legs[0] == first.legs[1]
    assert all(leg.model != blocked for leg in fallback.legs)
    assert any(s['leg'] == first.legs[0].id and s['reason'] == state
               for s in fallback.skipped)


def test_route_free_first_keys():
    from dataclasses import replace
    from plangraph.planner.legs import spawnable_legs, TIERS
    from plangraph.planner.review_order import review_key
    from plangraph.planner.route_policy import implement_key
    sol = next(l for l in spawnable_legs() if l.id == 'cx-gpt-6.2-sol')
    free = replace(sol, id='free-test', tier='free', family='gemini')
    for key in (review_key, implement_key):
        assert key(free, TIERS.index('free'), 1) < key(sol, TIERS.index('subscription'), 0)


def test_catalog_tiers_preserve_roles():
    rows = [{'id': 'agy/gemini-4-neon-medium', 'tier': 'free'}]
    updated = catalog_tiers(DEFAULT_LEGS, rows)
    assert next(l for l in updated if l.id == 'agy-gemini-4-neon-medium').tier == 'free'
    assert [l.roles for l in updated] == [l.roles for l in DEFAULT_LEGS]
    assert next(l for l in updated if l.id == 'deepseek').tier == 'paid'


def test_catalog_tiers_do_not_leak_between_harnesses():
    base = replace(DEFAULT_LEGS[0], model='shared', tier='subscription')
    legs = (replace(base, harness='agy'), replace(base, harness='opencode'))
    assert [l.tier for l in catalog_tiers(legs, [{'id': 'agy/shared', 'tier': 'free'}])] == [
        'free', 'subscription']
    assert [l.tier for l in catalog_tiers(legs, [{'id': 'shared', 'tier': 'paid'},
        {'id': 'agy/shared', 'tier': 'free'}])] == ['free', 'subscription']


def test_manual_disable_expiry():
    leg = replace(DEFAULT_LEGS[1], disabled='temporary operator stop', expires=160)
    assert not leg_active(leg, now=159)
    assert leg_active(leg, now=160)
    assert not leg_active(replace(leg, expires=None), now=100000)


def test_registered_pool_seats_exclude_claude_writers():
    ids = {'omniroute-deepseek-flash', 'omniroute-muse-spark',
           'agy-sonnet-5.5', 'agy-claude-opus'}
    legs = tuple(leg for leg in DEFAULT_LEGS if leg.id in ids)
    assert {leg.id for leg in legs} == ids
    plan = plan_spawn({'estimate_tokens': 1000}, role='review', legs=legs,
                      writer_families=('claude',), catalog_rows=(),
                      leg_reader=lambda *a, **k: {'state': 'ok', 'evidence': None})
    assert [leg.id for leg in plan.legs] == ['omniroute-deepseek-flash', 'omniroute-muse-spark']
    assert [leg.id for leg in plan.review_seats] == ['omniroute-deepseek-flash', 'omniroute-muse-spark']


@pytest.mark.parametrize('tier', ['free', 'subscription', 'paid'])
@pytest.mark.parametrize('source', ['id', 'model_id', 'provider_id'])
def test_catalog_tier_identity_and_route_output(tier, source):
    leg = next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-4-neon-medium')
    # Provider-qualified catalog identity must win over an unrelated bare name.
    rows = [{source: 'agy/' + leg.model, 'tier': tier},
            {source: leg.model, 'tier': 'paid' if tier != 'paid' else 'free'}]
    if source == 'provider_id':
        rows[0] = {'id': 'agy/served-alias', 'provider_id': 'agy', 'harness': None,
                   'upstream_id': leg.model, 'tier': tier}
        rows[1] = {'id': leg.model, 'tier': 'paid' if tier != 'paid' else 'free'}
    for role in ('implement', 'review'):
        projected, = catalog_tiers((replace(leg, roles=(role,)),), rows)
        output = SpawnPlan((projected,), (), 'test', (projected,), ()).to_dict()
        assert output['first_leg']['tier'] == tier
        assert output['legs'][0]['tier'] == tier
        assert output['review_seats'][0]['tier'] == tier
        assert projected.roles == (role,)


@pytest.mark.parametrize('tier', ['trial', 'free', 'subscription', 'paid'])
@pytest.mark.parametrize('role', ['implement', 'review'])
def test_catalog_tier_survives_planning_and_cli(tier, role, monkeypatch, tmp_path, capsys):
    import json
    from dataclasses import replace
    from plangraph.planner.legs import spawnable_legs, TIERS
    from plangraph.planner.routing import plan_spawn
    from plangraph.routing_support import resolver
    from plangraph.spawner.cli import main
    base = next(l for l in spawnable_legs() if l.id == 'cx-gpt-6.2-sol')
    legs = (replace(base, roles=(role,)), replace(base, id='free-review',
            model='test/free-review', family='gemini', tier='free', roles=(role,)))
    monkeypatch.setattr('plangraph.planner.legs.DEFAULT_LEGS', legs)
    monkeypatch.setitem(plan_spawn.__kwdefaults__, 'legs', legs)
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        {'id': l.model, 'tier': tier if l.id == base.id else 'free',
         'available': 1, 'active': True} for l in legs])
    card = {'estimate_tokens': 1000}
    plan = plan_spawn(card, role=role, writer_families=('claude',))
    assert next(l for l in plan.legs if l.id == base.id).tier == tier
    assert [TIERS.index(l.tier) for l in plan.legs] == sorted(TIERS.index(l.tier) for l in plan.legs)
    assert main(['--state', str(tmp_path), 'route', '--role', role,
                 '--writer-family', 'claude', '--card', json.dumps(card), '--json']) == 0
    output = json.loads(capsys.readouterr().out)
    for key in ('legs', 'candidates', 'review_seats') if role == 'review' else ('legs', 'candidates'):
        assert next(row for row in output[key] if row.get('id', row.get('leg')) == base.id)['tier'] == tier
    assert output['first_leg']['tier'] == plan.first_leg.tier
    assert all(l.roles == (role,) for l in plan.legs)


def test_old_limit_evidence_reprobes(store):
    store.record_provider_response('test/leg', 429, 'quota', {'Retry-After': '86400'}, 'test', now=100)
    assert ls.EVIDENCE_TTL_S == 6 * 60 * 60
    assert store.leg_status('test/leg', now=100 + ls.EVIDENCE_TTL_S - 1)['state'] == 'limited'
    assert store.leg_status('test/leg', now=100 + ls.EVIDENCE_TTL_S)['state'] == 'limited'
    assert store.leg_status('test/leg', now=100 + 86400 - 1)['state'] == 'limited'
    assert store.leg_status('test/leg', now=100 + 86400)['state'] == 'ok'
    # Also exercise a 7-day reset_at exceeding the 24 h flat down_s cap.
    with store._connect() as db:
        before = db.execute('SELECT reset_at FROM leg_events').fetchone()[0]
        db.execute('UPDATE leg_events SET reset_at=?', (100 + 7 * 86400,))
        assert db.execute('SELECT reset_at FROM leg_events').fetchone()[0] > before
    state = store.leg_status('test/leg', now=100 + 25 * 3600)
    assert state['state'] == 'limited'
    assert state['evidence']['reset_at'] == 100 + 7 * 86400
    # Reset-less limit evidence still expires to reprobe.
    store.record_provider_response('test/noreset', 429, 'quota', {}, 'test', now=100)
    assert store.leg_status('test/noreset', now=100 + 21600)['state'] == 'reprobe'


@pytest.mark.parametrize('source,status,expected_with_reset', [
    ('provider-probe', 200, 'ok'),
    ('provider-response', 429, 'limited'),
    ('harness-stderr', 429, 'limited'),
    ('liveness-probe', 503, 'reprobe'),
    ('unrecognized', 500, 'reprobe'),
])
def test_old_evidence_is_classified_before_reprobe(store, source, status, expected_with_reset):
    store.record_provider_response('test/source', 429, 'quota', {'Retry-After': '86400'}, 'test', now=100)
    with store._connect() as db:
        db.execute('UPDATE leg_events SET source=?, status=?', (source, status))
    assert store.leg_status('test/source', now=21700)['state'] == expected_with_reset
    with store._connect() as db:
        db.execute('UPDATE leg_events SET reset_at=NULL')
    assert store.leg_status('test/source', now=21700)['state'] == ('ok' if status == 200 else 'reprobe')


@pytest.mark.parametrize('state,expected', [('ok', True), ('reprobe', True), ('limited', False),
                                          ('down', False), ('expired', False)])
def test_resolver_uses_shared_unavailable_states(state, expected):
    from plangraph.routing_support.resolver import is_model_available
    from plangraph.routing_support.liveness import unavailable
    model = dict(id='test/model', available=1, active=True)
    assert (not unavailable(dict(state=state))) is expected
    def reader(*a, **k):
        return dict(state=state, evidence=None)
    # Main's disabled-reader compatibility path excludes limited only;
    # live authority admits exactly ok and cannot reuse reprobe/down evidence.
    assert is_model_available(model, reader) is (state != 'limited')
    reader.enabled = True
    assert is_model_available(model, reader) is (state == 'ok')


@pytest.mark.parametrize('age,reset', [(21600, 86400), (60, 60)])
def test_expired_probe_failure_cannot_complete_down_streak(store, age, reset):
    store.record_probe_fail('test/expiry', 'timeout', 'test', now=100, down_s=reset)
    store.record_probe_fail('test/expiry', 'timeout', 'test', now=100 + age, down_s=86400)
    assert store.leg_status('test/expiry', now=100 + age)['state'] == 'reprobe'
    store.record_probe_fail('test/expiry', 'timeout', 'test', now=101 + age, down_s=86400)
    assert store.leg_status('test/expiry', now=101 + age)['state'] == 'down'


def test_probe_failures_do_not_extend_hidden_limit_evidence(store):
    store.record_provider_response('test/leg', 429, 'quota', {'Retry-After': '86400'}, 'test', now=100)
    store.record_probe_fail('test/leg', 'timeout', 'probe', now=200, down_s=ls.RESET_CAP_S)
    assert store.leg_status('test/leg', now=100 + ls.EVIDENCE_TTL_S - 1)['state'] == 'limited'
    assert store.leg_status('test/leg', now=100 + ls.EVIDENCE_TTL_S)['state'] == 'limited'
    assert store.leg_status('test/leg', now=100 + 86400)['state'] == 'reprobe'
    store.record_probe_fail('test/down', 'timeout', 'probe', now=100, down_s=ls.RESET_CAP_S)
    store.record_probe_fail('test/down', 'timeout', 'probe', now=101, down_s=ls.RESET_CAP_S)
    assert store.leg_status('test/down', now=101 + ls.EVIDENCE_TTL_S - 1)['state'] == 'down'
    assert store.leg_status('test/down', now=101 + ls.EVIDENCE_TTL_S)['state'] == 'reprobe'


@pytest.fixture
def store(tmp_path):
    return ls.LegStateStore(tmp_path / "legs.sqlite")


@pytest.mark.parametrize("status", [429, 402])
def test_reset(store, status):
    store.record_provider_response("cx/gpt-6.2-sol", status, "quota", {"Retry-After": "60"}, "gateway", now=100)
    result = store.leg_status("cx/gpt-6.2-sol", now=159)
    assert result["state"] == "limited"
    assert result["evidence"]["status"] == status
    assert result["evidence"]["reset_at"] == 160
    assert store.leg_status("cx/gpt-6.2-sol", now=160)["state"] == "ok"


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
            return [dict(id=leg, available=1, active=True, aa_intelligence=50, cost_per_task_usd=cost, context_window=1000000) for leg, cost in [("cx/gpt-6.2-sol", 0), ("other/sol", 1)]]
    store.record_provider_response("cx/gpt-6.2-sol", 429, "quota", {"Retry-After": "60"}, "gateway", now=100, combo="l2-worker")
    def route(now):
        return resolve_model_for_task(db=Catalog(), leg_state_reader=partial(store.leg_status, now=now))
    result = route(101)
    assert result["model"] == "other/sol"
    assert result["leg_state"]["state"] == "ok"
    assert result["candidates"][0]["leg_state"]["evidence"]["status"] == 429
    assert route(160)["model"] == "cx/gpt-6.2-sol"


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
    ("(reset after 4d 2h)", 4 * 86400 + 2 * 3600),
])
def test_reset_after_formats(store, body, delta):
    """D-3685/D-6082: OmniRoute's "reset after" books like "resets in"; an elapsed
    cooldown ("reset after 0s") books nothing at all and a far-future provider
    reset is stored and honoured uncapped."""
    store.record_provider_response("cx/zz-model", 429, body, {}, "gateway", now=100)
    state = store.leg_status("cx/zz-model", now=101)
    if delta is None:
        assert state["state"] == "ok"  # no reset-less limit row was written
    else:
        assert state["evidence"]["reset_at"] == 100 + delta
        assert store.leg_status("cx/zz-model", now=100 + delta / 2)["state"] == "limited"
        assert store.leg_status("cx/zz-model", now=100 + delta)["state"] == "ok"


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


def test_provider_resets_are_uncapped_while_flat_down_s_stays_capped(store, monkeypatch):
    """D-6082/D-6143: parsed agy and opencode-go resets are booked and honoured in full;
    flat fallback down_s stays capped at 24 h."""
    now = 1_795_000_000.0
    store.record_harness_limit(
        "agy/gemini-4-hydrogen-high",
        "RESOURCE_EXHAUSTED (code 429): Per-model quota reached. Resets in 135h6m15s",
        "spawner:harness",
        now=now,
        down_s=1800,
    )
    h_state = store.leg_status("agy/gemini-4-hydrogen-high", now=now + 1)
    assert h_state["evidence"]["reset_at"] == now + 486375
    assert store.leg_status("agy/gemini-4-hydrogen-high", now=now + 25 * 3600)["state"] == "limited"
    assert store.leg_status("agy/gemini-4-hydrogen-high", now=now + 486374)["state"] == "limited"
    assert store.leg_status("agy/gemini-4-hydrogen-high", now=now + 486375)["state"] == "ok"

    store.record_provider_response(
        "agy/claude-sonnet-5-5-high",
        429,
        "Individual quota reached. Resets in 97h31m40s",
        {},
        "gateway",
        now=now,
    )
    assert store.leg_status("agy/claude-sonnet-5-5-high", now=now + 25 * 3600)["state"] == "limited"
    assert store.leg_status("agy/claude-sonnet-5-5-high", now=now + 97 * 3600 + 31 * 60 + 40)["state"] == "ok"

    store.record_harness_limit(
        "opencode-go/beam-free",
        "HTTP 429: usage_limit (reset after 176616000ms)",
        "spawner:harness",
        now=now,
        down_s=1800,
    )
    go_state = store.leg_status("opencode-go/beam-free", now=now + 1)
    assert go_state["evidence"]["reset_at"] == now + 176616
    assert store.leg_status("opencode-go/beam-free", now=now + 7 * 3600)["state"] == "limited"
    assert store.leg_status("opencode-go/beam-free", now=now + 25 * 3600)["state"] == "limited"
    assert store.leg_status("opencode-go/beam-free", now=now + 176616)["state"] == "ok"
    # A later probe failure never hides an uncapped provider limit after 6 h.
    store.record_probe_fail("opencode-go/beam-free", "timeout", "leg-probe", now=now + 60, down_s=60)
    assert store.leg_status("opencode-go/beam-free", now=now + 7 * 3600)["state"] == "limited"

    store.record_harness_limit("opencode-go/flat", "error: quota threshold", "spawner:harness", now=now, down_s=172800)
    assert store.leg_status("opencode-go/flat", now=now + 1)["evidence"]["reset_at"] == now + ls.RESET_CAP_S
    monkeypatch.setenv("PLANGRAPH_LEG_DOWN_S", "172800")
    store.record_probe_fail("opencode-go/flat-probe", "timeout", "leg-probe", now=now)
    assert store.leg_status("opencode-go/flat-probe", now=now + 1)["evidence"]["reset_at"] == now + ls.RESET_CAP_S


def test_argon_efforts_share_one_agy_quota_key(tmp_path):
    """D-5966 / rule 3.58: a limit on argon-low is the argon-medium limit too."""
    from plangraph.routing_support.leg_state import LegAdapter, LegStateStore, quota_key
    assert quota_key('agy/gemini-4-argon-low') == quota_key('agy:gemini-4-argon-medium')
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_harness_limit('agy/gemini-4-argon-low', 'Individual quota reached. Resets in 2h.', 'fixture')
    assert store.leg_status('agy/gemini-4-argon-medium')['state'] == 'limited'
    assert store.leg_status('agy:gemini-4-argon')['state'] == 'limited'
    adapter = LegAdapter(store, None, lambda: (), enabled=True, clock=lambda: 0)
    assert adapter.leg_status('agy/gemini-4-argon-medium')['state'] == 'limited'
    assert adapter.leg_status('gemini-4-argon-medium') == store.leg_status('gemini-4-argon-medium')  # store, not the gateway


@pytest.mark.parametrize('effort', ['', '-low', '-medium', '-high', '-xhigh', '-max'])
@pytest.mark.parametrize('separator', ['/', ':'])
def test_quota_key_gemini_pool(effort, separator):
    for model in ('gemini-4-argon', 'gemini-4-hydrogen', 'gemini-4-neon', 'claude-sonnet-5-5'):
        expected = 'gemini-4-hydrogen' if model == 'gemini-4-argon' else model
        assert ls.quota_key(f'agy{separator}{model}{effort}') == f'agy:{expected}'
        assert ls.quota_key(model + effort) == model + effort


def test_argon_429_limits_hydrogen(store):
    store.record_provider_response('agy/gemini-4-argon-low', 429, 'quota',
                                   {'Retry-After': '60'}, 'fixture', now=100)
    assert store.leg_status('agy/gemini-4-hydrogen-high', now=159)['state'] == 'limited'
    assert store.leg_status('agy/gemini-4-hydrogen-high', now=160)['state'] == 'ok'


def test_hydrogen_429_limits_argon(store):
    store.record_harness_limit('agy/gemini-4-hydrogen-high',
                               'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 2h.',
                               'fixture', now=100, harness='agy')
    assert store.leg_status('agy/gemini-4-hydrogen-medium', now=101)['state'] == 'limited'
    assert store.leg_status('agy/gemini-4-argon-low', now=101)['state'] == 'limited'
    assert store.leg_status('agy/gemini-4-argon-low', now=7300)['state'] == 'ok'
    for model in ('agy/gemini-4-neon-medium', 'agy/claude-sonnet-5-5-high'):
        assert store.leg_status(model, now=101)['state'] == 'ok'


@pytest.mark.parametrize('model,sibling', [('gemini-4-neon', 'claude-sonnet-5-5'),
                                          ('claude-sonnet-5-5', 'gemini-4-neon')])
def test_agy_429_applies_only_to_the_model_that_was_called(store, model, sibling):
    store.record_harness_limit(f'agy/{model}-medium', 'HTTP 429: quota reached',
                               'fixture', now=100, harness='agy')
    assert store.leg_status(f'agy/{model}-medium', now=101)['state'] == 'limited'
    for other in (sibling, 'gemini-4-hydrogen', 'gemini-4-argon'):
        assert store.leg_status(f'agy:{other}', now=101)['state'] == 'ok'


@pytest.mark.parametrize('older_key', ['agy:gemini-4-argon', 'agy/gemini-4-argon-low',
                                      'agy:gemini-4-argon-medium', 'agy/gemini-4-hydrogen-max'])
def test_pre_pool_argon_booking_counts(store, older_key):
    store.record_provider_response('agy:gemini-4-hydrogen', 429, 'quota',
                                   {'Retry-After': '60'}, 'fixture', now=100, account='A')
    with store._connect() as db:
        db.execute('UPDATE leg_events SET leg_key=?', (older_key,))
    assert store.leg_status('agy:gemini-4-hydrogen', now=101, account='A')['state'] == 'limited'
    assert store.leg_status('agy:gemini-4-hydrogen', now=101, account='B')['state'] == 'ok'
    assert store.agy_probe_status('agy:gemini-4-hydrogen', now=101)['state'] == 'limited'


def test_overlapping_pre_pool_bookings_hold_until_both_reset(store):
    argon, hydrogen = 'agy:gemini-4-argon', 'agy:gemini-4-hydrogen'
    store.record_provider_response(argon, 429, 'quota', {'Retry-After': '60'}, 'fixture', now=100)
    with store._connect() as db:
        db.execute('UPDATE leg_events SET leg_key=?', (argon,))
    store.record_provider_response(hydrogen, 429, 'quota', {'Retry-After': '10'}, 'fixture', now=101)
    for model in (argon, hydrogen):
        status = store.leg_status(model, now=111)
        assert status['state'] == 'limited' and status['evidence']['reset_at'] == 160
        assert store.leg_status(model, now=160)['state'] == 'ok'


def test_failed_agy_reprobe_preserves_a_recent_429_without_reset(store):
    model = 'agy/gemini-4-argon-low'
    store.record_provider_response(model, 429, 'quota', {}, 'fixture', now=100)
    store.record_probe_fail(model, 'review probe failed', 'review-probe', now=101)
    assert store.leg_status(model, now=102)['state'] == 'limited'


def test_gemini_probe_failure_stays_local_but_recovery_clears_the_pool(store):
    argon, hydrogen = 'agy/gemini-4-argon-low', 'agy/gemini-4-hydrogen-high'
    store.record_probe_fail(argon, 'timeout', 'review-probe', now=100, down_s=60)
    assert store.leg_status(argon, now=101)['state'] == 'down'
    assert store.leg_status(hydrogen, now=101)['state'] == 'ok'
    assert store.last_probe_attempt_ts(argon, now=101) == 100
    assert store.last_probe_attempt_ts(hydrogen, now=101) is None

    store.record_provider_response(hydrogen, 429, 'quota', {'Retry-After': '60'}, 'fixture', now=101)
    assert store.leg_status(argon, now=102)['state'] == 'limited'
    store.record_probe_pass(argon, 'review-probe', now=103)
    assert store.leg_status(argon, now=104)['state'] == 'ok'
    assert store.leg_status(hydrogen, now=104)['state'] == 'ok'
