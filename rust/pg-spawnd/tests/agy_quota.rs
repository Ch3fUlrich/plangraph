//! Exercise the shared routing/store boundary used by the Rust route adapter.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use serde_json::Value;

#[test]
fn hydrogen_and_argon_share_one_exposed_limit() {
    let state = tempfile::tempdir().unwrap();
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("XDG_STATE_HOME", state.path())
        .env("PLANGRAPH_OMNIROUTE_LEG_STATE", "0")
        .args([
            "-c",
            r#"
import json
from plangraph.routing_support.leg_state import _default_store, LegStateStore
from plangraph.planner.routing import plan_spawn
store = _default_store()
card = dict(estimate_tokens=1000, paths=['rust/pg-spawnd/src/spawn.rs'], has_tests=True)
def route():
    return plan_spawn(card, role='review', writer_families=['openai'],
                      leg_reader=store, catalog_rows=[]).to_dict()
initial = route()
store.record_harness_limit('agy/gemini-4-hydrogen-high',
    'RESOURCE_EXHAUSTED (code 429): Per-model quota reached. Resets in 10h40m26s.', 'fixture')
hydrogen = route()
store.record_harness_limit('agy/gemini-4-argon-medium',
    'Per-model quota reached. Resets in 2h.', 'fixture')
argon = route()
store.record_harness_limit('agy/gemini-4-hydrogen-medium',
    'RESOURCE_EXHAUSTED (code 429): Per-model quota reached. Resets in 10h40m26s.', 'fixture')
store.record_harness_limit('claude-sonnet-5-5-high',
    'Individual quota reached. Resets in 1h.', 'fixture')
store.record_harness_limit('agy/claude-sonnet-5-5-high',
    'Per-model quota reached. Resets in 1h.', 'fixture')
legacy = LegStateStore(store.path.with_name('legacy.sqlite'))
with legacy._connect() as db:
    db.execute("INSERT INTO leg_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
               ('agy/gemini-4-argon-low', None, None, 429, 'old quota', 36000, 36010, 10, 'fixture', 'harness-stderr'))
legacy_state = legacy.leg_status('agy:gemini-4-hydrogen', now=7*3600)
snapshot = plan_spawn(card, role='review', writer_families=['openai'], now=42, catalog_rows=[],
    leg_reader=lambda model, **kw: dict(state='limited' if 'gemini-4-argon' in model else 'ok', evidence=dict(ts=kw['now']))).to_dict()
gemini_only = plan_spawn(card, role='review', writer_families=['openai'], catalog_rows=[],
    leg_reader=lambda model, **kw: dict(state='ok' if any(name in model for name in ('gemini-4-hydrogen', 'gemini-4-argon')) else 'limited', evidence=None)).to_dict()
print(json.dumps(dict(snapshot=snapshot, legacy=legacy_state, gemini_only=gemini_only, initial=initial, hydrogen=hydrogen, argon=argon, final=route(), later=store.leg_status('agy:gemini-4-hydrogen', now=store.leg_status('agy:gemini-4-hydrogen')['evidence']['ts'] + 7*3600))))
"#,
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    let models_of = |stage: &str| -> Vec<&str> {
        result[stage]["legs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|leg| leg["model"].as_str().unwrap())
            .collect()
    };
    // D-6652: the two required OpenCode seats lead, so a Hydrogen or Argon limit never changes the first leg.
    for stage in ["initial", "hydrogen", "argon"] {
        assert_eq!(
            result[stage]["first_leg"]["model"],
            "opencode-go/deepseek-v4.1-flash"
        );
    }
    let initial = models_of("initial");
    assert_eq!(
        initial,
        [
            "opencode-go/deepseek-v4.1-flash",
            "opencode-go/glm-5.3-flash",
            "gemini-4-hydrogen-high",
            "mimo/mimo-v2.6-pro", // the MiMo Pro review leg with read-only tools
            "opencode/deepseek-v4.1-flash-free",
            "gemini-4-argon-low",
            "gemini-4-argon-medium"
        ]
    );
    let without_pool: Vec<&str> = initial
        .iter()
        .copied()
        .filter(|model| {
            !model.starts_with("gemini-4-hydrogen") && !model.starts_with("gemini-4-argon")
        })
        .collect();
    for stage in ["hydrogen", "argon", "snapshot"] {
        assert_eq!(models_of(stage), without_pool);
        let states = &result[stage]["agy_quota_states"];
        assert_eq!(states["agy:gemini-4-hydrogen"]["state"], "limited");
        assert_eq!(
            states["agy:gemini-4-argon"],
            states["agy:gemini-4-hydrogen"]
        );
    }
    let argon_states = &result["argon"]["agy_quota_states"];
    assert_eq!(argon_states["agy:gemini-4-hydrogen"]["state"], "limited");
    assert_eq!(argon_states["agy:gemini-4-argon"]["state"], "limited");
    assert_eq!(result["later"]["state"], "limited");
    assert_eq!(
        result["snapshot"]["agy_quota_states"]["agy:gemini-4-argon"]["state"],
        "limited"
    );
    assert_eq!(
        result["snapshot"]["agy_quota_states"]["agy:gemini-4-argon"]["evidence"]["ts"],
        42
    );
    assert!(!result["final"]["legs"]
        .as_array()
        .unwrap()
        .iter()
        .any(|l| l["model"] == "claude-sonnet-5-5-high"));
    assert_eq!(result["legacy"]["state"], "limited");
    assert_eq!(
        result["gemini_only"]["first_leg"]["model"],
        "gemini-4-hydrogen-high"
    );
    // D-6652: every head needs two seats from distinct families.
    assert_eq!(
        result["gemini_only"]["error"],
        "two distinct review families unavailable"
    );
    let states = &result["hydrogen"]["agy_quota_states"];
    assert_eq!(states["agy:gemini-4-hydrogen"]["state"], "limited");
    assert_eq!(states["agy:claude-sonnet-5-5"]["state"], "ok");
    assert_eq!(states.as_object().unwrap().len(), 3);
    for state in result["final"]["agy_quota_states"]
        .as_object()
        .unwrap()
        .values()
    {
        assert!(
            state["evidence"]["reset_at"].as_f64().unwrap()
                > state["evidence"]["ts"].as_f64().unwrap()
        );
    }
}

#[test]
fn native_sonnet_and_agy_quota_keys_are_independent() {
    let state = tempfile::tempdir().unwrap();
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args([
            "-c",
            r#"
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from plangraph.routing_support.leg_state import LegStateStore, quota_key
from plangraph.planner.legs import REVIEW_FALLBACK_LEGS
from plangraph.planner.routing import plan_spawn, SpawnPlan, SpawnRefused, next_leg_after_provider_stop
from plangraph.routing_support.liveness import require_available
from plangraph.spawner.exit_outcome import fallthrough_reason
from plangraph.spawner.leg_probe import DispatchReader

initial = plan_spawn(dict(estimate_tokens=1000), role='review', writer_families=['openai'],
                     legs=REVIEW_FALLBACK_LEGS, leg_reader=lambda *a, **kw: dict(state='ok', evidence=None), catalog_rows=[])
native = next(leg for leg in initial.legs if leg.id == 'claude-sonnet-5-5-high')
assert native.harness == 'claude' and native.tier == 'subscription'
agy = 'agy/claude-sonnet-5-5-high'
store = LegStateStore(Path(sys.argv[1]) / 'agy.sqlite')
store.record_provider_response(agy, 429, 'Individual quota reached. Resets in 97h.', {}, 'fixture', now=100)
assert store.leg_status(native.model, now=101)['state'] == 'ok'
assert DispatchReader(store, None).leg_status(native.model, now=101)['state'] == 'ok'
assert quota_key(native.model) == native.model
for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
    assert quota_key('agy/claude-sonnet-5-5-' + effort) == 'agy:claude-sonnet-5-5'
    assert store.leg_status('agy/claude-sonnet-5-5-' + effort, now=101)['state'] == 'limited'
plan = plan_spawn(dict(estimate_tokens=1000), role='review', writer_families=['openai'],
                  legs=(native,), leg_reader=DispatchReader(store, None), catalog_rows=[], now=101)
assert plan.first_leg == native, plan.to_dict()
assert plan.to_dict()['agy_quota_states']['agy:claude-sonnet-5-5']['state'] == 'limited'

store = LegStateStore(Path(sys.argv[1]) / 'native.sqlite')
job = NS(exit_code=1, stdout='', stderr='Error: 429 rate limit reached. Resets in 1h.')
reason = fallthrough_reason(NS(leg_reader=store), native, job, {})
assert reason.startswith('harness_limit:'), reason
assert store.leg_status(native.model)['state'] == 'limited'
assert store.leg_status(agy)['state'] == 'ok'
assert store.leg_status('claude-opus-5-5-high')['state'] == 'ok'
with store._connect() as db:
    assert [row['leg_key'] for row in db.execute('SELECT leg_key FROM leg_events')] == [native.model]
plan = plan_spawn(dict(estimate_tokens=1000), role='review', writer_families=['openai'],
                  legs=(native,), leg_reader=store, catalog_rows=[])
assert plan.to_dict()['agy_quota_states']['agy:claude-sonnet-5-5']['state'] == 'ok'
assert not plan.legs
assert any(row['leg'] == native.id and row['reason'] == 'limited' for row in plan.skipped)
agy_leg = replace(native, id='agy-sonnet-fixture', harness='agy')
plan = plan_spawn(dict(estimate_tokens=1000), role='review', writer_families=['openai'],
                  legs=(native, agy_leg), leg_reader=DispatchReader(store, None), catalog_rows=[])
assert plan.legs == (agy_leg,), plan.to_dict()
require_available(agy_leg, store, {})
try:
    require_available(native, store, {})
except SpawnRefused:
    pass
else:
    raise AssertionError('native limit did not refuse native override')
store = LegStateStore(Path(sys.argv[1]) / 'override.sqlite')
store.record_harness_limit(agy, 'Error: 429 quota reached. Resets in 1h.', 'fixture')
require_available(native, store, {})
try:
    require_available(agy_leg, store, {})
except SpawnRefused:
    pass
else:
    raise AssertionError('agy limit did not refuse agy override')
store = LegStateStore(Path(sys.argv[1]) / 'provider.sqlite')
plan = SpawnPlan((agy_leg, native), (), 'fixture', (), ())
assert next_leg_after_provider_stop(plan, agy_leg.id, 429, 'quota reached',
                                    {'Retry-After': '3600'}, 'fixture', store=store) == native
assert store.leg_status(native.model)['state'] == 'ok'
assert store.leg_status(agy)['state'] == 'limited'
store = LegStateStore(Path(sys.argv[1]) / 'native-provider.sqlite')
plan = SpawnPlan((native, agy_leg), (), 'fixture', (), ())
assert next_leg_after_provider_stop(plan, native.id, 429, 'quota reached',
                                    {'Retry-After': '3600'}, 'fixture', store=store) == agy_leg
assert store.leg_status(native.model)['state'] == 'limited'
assert store.leg_status(agy)['state'] == 'ok'
"#,
            state.path().to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn provider_stated_resets_are_booked_and_honoured_uncapped() {
    let state = tempfile::tempdir().unwrap();
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("XDG_STATE_HOME", state.path())
        .env("PLANGRAPH_HOST", "fixture-host")
        .args([
            "-c",
            r#"
import json, os, sys
from pathlib import Path
from plangraph.routing_support.leg_state import LegStateStore, RESET_CAP_S
from plangraph.planner.measured_down import measured_down
from plangraph.planner.routing import plan_spawn

root = Path(sys.argv[1])
store = LegStateStore(root / 'plangraph' / 'leg-state.sqlite')
now = 1_795_000_000.0
store.record_harness_limit(
    'agy/gemini-4-hydrogen-high',
    'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 135h6m15s',
    'fixture', now=now, down_s=1800)
assert store.leg_status('agy/gemini-4-hydrogen-high', now=now + 1)['evidence']['reset_at'] == now + 486375
assert store.leg_status('agy/gemini-4-hydrogen-high', now=now + 25 * 3600)['state'] == 'limited'
store.record_harness_limit(
    'opencode-go/beam-free',
    'HTTP 429: usage_limit (reset after 176616000ms)',
    'fixture', now=now, down_s=1800)
assert store.leg_status('opencode-go/beam-free', now=now + 7 * 3600)['state'] == 'limited'
store.record_harness_limit('opencode-go/flat', 'error: quota threshold', 'fixture', now=now, down_s=172800)
assert store.leg_status('opencode-go/flat', now=now + 1)['evidence']['reset_at'] == now + RESET_CAP_S

rec = root / 'runs' / 'run-fixture' / 'record.json'
rec.parent.mkdir(parents=True)
rec.write_text(json.dumps(dict(host='fixture-host', ended=now, attempts=[
    dict(status='failed', down_reason='usage_limit',
         leg=dict(id='agy-gemini-4-hydrogen', model='gemini-4-hydrogen-high', harness='agy')),
    dict(status='failed', down_reason='usage_limit',
         leg=dict(id='opencode-go-beam-free', model='opencode-go/beam-free', harness='opencode')),
])))
os.utime(rec, (now, now))
down_25h = measured_down(root, now=now + 25 * 3600)
assert 'agy-gemini-4-hydrogen' in down_25h and 'opencode-go-beam-free' in down_25h
plan = plan_spawn(dict(estimate_tokens=1000), role='review', writer_families=['openai'],
                  leg_reader=store, catalog_rows=[], now=now + 25 * 3600)
ids = {leg.id for leg in plan.legs}
assert 'agy-gemini-4-hydrogen' not in ids and 'opencode-go-beam-free' not in ids
"#,
            state.path().to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
