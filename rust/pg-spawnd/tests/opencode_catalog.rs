//! Check catalog identities against the same overlay and renderer used at launch.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use serde_json::{json, Value};
use std::path::PathBuf;

const CATALOG_SCRIPT: &str = r#"
import json
from dataclasses import replace
from unittest.mock import patch
from plangraph.planner.legs import spawnable_legs, REVIEW_FALLBACK_LEGS
from plangraph.routing_support import resolver
from plangraph.routing_support.opencode_overlay import overlay_env
from plangraph.routing_support.opencode_render import render_gateway_providers

legs = {leg.id: leg for leg in (*spawnable_legs(), *REVIEW_FALLBACK_LEGS)}
legs['unknown-model-regression'] = replace(legs['omniroute-muse-spark'],
    id='unknown-model-regression', model='omniroute/unknown-model')
rows = []
# Use shipped configuration and fixture endpoints, never operator files or live probes.
with patch.object(resolver, 'host_probe_env', lambda env: overlay_env(env)), \
     patch('plangraph.routing_support.clients.effective_gateway_base_url',
           return_value='https://gateway.example/v1'):
    for leg in legs.values():
        if leg.harness != 'opencode' or leg.disabled:
            continue
        env = {'PLANGRAPH_GATEWAY_URL': 'https://gateway.example/v1'}
        child = overlay_env(env)
        render_reason = render_gateway_providers(child, leg.model)
        providers = json.loads(child['OPENCODE_CONFIG_CONTENT'])['providers']
        served = {provider + '/' + model for provider, block in providers.items()
                  for model in block.get('models', {})}
        reason = resolver.host_skip_reason(
            {'harness': leg.harness, 'model': leg.model},
            env=env, probe=lambda _: served)
        rows.append({'leg': leg.id, 'model': leg.model,
                     'declared': leg.model in served,
                     'reason': render_reason or reason})
print(json.dumps(rows))
"#;

/// Run a script in the checkout against the shipped provider file; it prints one JSON value.
fn python_json<T: serde::de::DeserializeOwned>(script: &str) -> T {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .args(["-c", script])
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env(
            "PLANGRAPH_PROVIDERS_FILE",
            root.join("configuration/opencode/providers.json"),
        )
        .output()
        .expect("run python script");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("script JSON")
}

fn catalog() -> Vec<Value> {
    python_json(CATALOG_SCRIPT)
}

// D-6054: named exclusions for externally supplied catalogs; no provider-wide bypass.
// Grok's missing declaration remains owned by PG-OMNIROUTE-CONFIG-HOME.
const EXCLUDED: &[&str] = &[
    "openrouter-free",
    "opencode-flint-40b-free",
    "opencode-big-pickle",
    "opencode-go-qwen3.8-max",
    "opencode-go-muse-spark",
    "opencode-console-muse-spark",
    "opencode-go-deepseek-v4-flash",
    "opencode-go-glm-5.3-flash",
    "console-deepseek-flash",
    "opencode-go-beam-free",
    "omniroute-grok-4.7",
];

fn check(row: &Value) -> Result<(), String> {
    if row["declared"] == true && row["reason"].is_null() {
        Ok(())
    } else {
        Err(format!(
            "leg {}: model {} missing from rendered config or refused: {}",
            row["leg"].as_str().unwrap(),
            row["model"].as_str().unwrap(),
            row["reason"]
        ))
    }
}

#[test]
fn every_enabled_opencode_leg_resolves_in_rendered_config() {
    let rows = catalog();
    for name in EXCLUDED {
        assert!(
            rows.iter().any(|row| row["leg"] == *name),
            "stale exclusion: {name}"
        );
    }
    let failures: Vec<_> = rows
        .iter()
        .filter(|row| row["leg"] != "unknown-model-regression")
        .filter(|row| !EXCLUDED.contains(&row["leg"].as_str().unwrap()))
        .filter_map(|row| check(row).err())
        .collect();
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn omniroute_muse_resolves_on_fixture_host() {
    let rows = catalog();
    let muse = rows
        .iter()
        .find(|row| row["leg"] == "omniroute-muse-spark")
        .unwrap();
    assert_eq!(muse["model"], "omniroute/spark-1.3-contributor");
    check(muse).unwrap();
}

#[test]
fn unknown_model_check_names_the_leg() {
    let rows = catalog();
    let unknown = rows
        .iter()
        .find(|row| row["leg"] == "unknown-model-regression")
        .unwrap();
    assert_eq!(unknown["declared"], false);
    assert_eq!(
        unknown["reason"],
        "not_served_on_host: omniroute/unknown-model"
    );
    let error = check(unknown).unwrap_err();
    assert!(error.contains("leg unknown-model-regression:"), "{error}");
    assert!(error.contains("omniroute/unknown-model"), "{error}");
}

// The catalog row owns the local prefix -> gateway provider slug pair. Both scripts replace
// the slug of the `mimo` row, so a hard-coded `xiaomi-mimo` anywhere would fail them.
const MIMO_PRELUDE: &str = r#"
import json, tempfile
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.routing_support import catalog_seed, leg_state

MODELS = ('mimo/mimo-v2.6-pro', 'mimo/mimo-v2.6-flash')
SLUG = 'gateway-mimo-slug'
state = lambda **kw: SimpleNamespace(**{**dict(available=True, stale=False, reason='', until=''), **kw})

def with_slug(slug, extra=()):
    rows = [dict(r, omniroute_id=slug) if r['provider_id'] == 'mimo' else r
            for r in catalog_seed.SEED_PROVIDERS]
    return patch.object(catalog_seed, 'SEED_PROVIDERS', rows + list(extra))

def status(snapshot, leg):
    store = leg_state.LegStateStore(tempfile.mkdtemp())
    adapter = leg_state.LegAdapter(store, SimpleNamespace(snapshot=lambda: snapshot),
                                   lambda: list(MODELS), enabled=True, clock=lambda: 0)
    return adapter.leg_status(leg)

from plangraph.spawner import gateway_witness as gw
class Runner:
    def __init__(self): self._adapter_tokens, self.calls = {'j': object()}, []
    def served_witness(self, job_id, model, *, adapter_token, finish_reason=None):
        self.calls.append(model)

def witnessed(rows):
    cfg = {'providers': {'omniroute': {'options': {}}}}
    gw.tag_session(cfg, 'j')
    job = SimpleNamespace(model_or_combo='omniroute/cx-gpt-6-luna', served_model=None, served_family=None,
                          served_witness_reason=None, witness_kind=None, events=[])
    runner = Runner()
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg), 'XDG_CONFIG_HOME': tempfile.mkdtemp()}
    gw.witness(runner, 'j', job, env, delay=0, attempts=1, fetch=lambda tag, env: rows)
    return runner.calls

tag = gw.session_tag('j')
"#;

fn mimo_script(body: &str) -> String {
    format!("{MIMO_PRELUDE}\n{body}")
}

#[test]
fn mimo_snapshot_and_tagged_call_log_share_provider_identity() {
    let got: Value = python_json(&mimo_script(
        r#"
out = {'quota': {}, 'witness': {}}
with with_slug(SLUG):
    for leg in MODELS:
        name = leg.split('/')[1]
        out['quota'][leg] = [status({SLUG: state()}, leg)['state'],
                             status({'mimo': state(), 'xiaomi-mimo': state()}, leg)['reason']]
        row = lambda **kw: dict(sessionTag=tag, status=200, **kw)
        out['witness'][leg] = [witnessed([row(model=name, provider=SLUG)]),
                               witnessed([row(model=SLUG + '/' + name)]),
                               witnessed([row(model=name, provider='xiaomi-mimo')])]
print(json.dumps(out))
"#,
    ));
    for (leg, name) in [
        ("mimo/mimo-v2.6-pro", "mimo-v2.6-pro"),
        ("mimo/mimo-v2.6-flash", "mimo-v2.6-flash"),
    ] {
        let local = format!("mimo/{name}");
        // The substituted slug is found; the old hard-coded one is not.
        assert_eq!(got["quota"][leg], json!(["ok", "unreachable"]), "{leg}");
        // Bare and qualified rows give the exact local model; an unclaimed slug stays itself.
        assert_eq!(
            got["witness"][leg],
            json!([[local], [local], [format!("xiaomi-mimo/{name}")]]),
            "{leg}"
        );
    }
}

#[test]
fn mimo_provider_identity_preserves_quota_state() {
    let got: Value = python_json(&mimo_script(
        r#"
import time
future = '2999-01-01T00:00:00+00:00'
decoy = state()   # a healthy row under the local prefix must never answer for the leg
cases = {
    'stale': state(stale=True),
    'limited': state(available=False, reason='rate_limited', until=future),
    'inactive': state(available=False, reason='inactive'),
}
out = {}
for leg in MODELS:
    for name, row in cases.items():
        got = status({'mimo': decoy, 'xiaomi-mimo': row}, leg)
        out[leg + ' ' + name] = [got['state'], got['reason'], bool((got['evidence'] or {}).get('reset_at'))]
    got = status({'mimo': decoy, 'other': state()}, leg)
    out[leg + ' missing'] = [got['state'], got['reason'], got['evidence']]
print(json.dumps(out))
"#,
    ));
    for leg in ["mimo/mimo-v2.6-pro", "mimo/mimo-v2.6-flash"] {
        assert_eq!(
            got[format!("{leg} stale")],
            json!(["unknown", "stale", false])
        );
        assert_eq!(
            got[format!("{leg} limited")],
            json!(["limited", "rate_limited", true])
        );
        assert_eq!(
            got[format!("{leg} inactive")],
            json!(["down", "provider_inactive", false])
        );
        assert_eq!(
            got[format!("{leg} missing")],
            json!(["down", "unreachable", {"looked_up_provider": "xiaomi-mimo"}])
        );
    }
}

#[test]
fn mimo_ambiguous_provider_slug_stays_unwitnessed() {
    let got: Value = python_json(&mimo_script(
        r#"
twin = dict(provider_id='mimo-twin', omniroute_id=SLUG)
row = lambda **kw: dict(sessionTag=tag, status=200, **kw)
out = {}
with with_slug(SLUG, [twin]):
    for name in ('mimo-v2.6-pro', 'mimo-v2.6-flash'):
        out[name] = [witnessed([row(model=name, provider=SLUG)]),
                     witnessed([row(model=SLUG + '/' + name)]),
                     witnessed([row(model=SLUG + '/' + name, provider=SLUG)])]
with with_slug(SLUG):   # the same row is witnessed once one catalog row owns the slug
    out['owned'] = witnessed([row(model='mimo-v2.6-pro', provider=SLUG)])
print(json.dumps(out))
"#,
    ));
    for name in ["mimo-v2.6-pro", "mimo-v2.6-flash"] {
        assert_eq!(got[name], json!([[], [], []]), "{name}");
    }
    assert_eq!(got["owned"], json!(["mimo/mimo-v2.6-pro"]));
}
