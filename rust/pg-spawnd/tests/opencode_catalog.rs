//! Check catalog identities against the same overlay and renderer used at launch.
use serde_json::Value;
use std::{path::PathBuf, process::Command};

fn catalog() -> Vec<Value> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .args([
            "-c",
            r#"
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
"#,
        ])
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env(
            "PLANGRAPH_PROVIDERS_FILE",
            root.join("configuration/opencode/providers.json"),
        )
        .output()
        .expect("run catalog renderer");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("catalog JSON")
}

// D-6054: named exclusions for externally supplied catalogs; no provider-wide bypass.
// Grok's missing declaration remains owned by PG-OMNIROUTE-CONFIG-HOME.
const EXCLUDED: &[&str] = &[
    "openrouter-free",
    "opencode-big-pickle",
    "opencode-gemini-4-argon",
    "opencode-go-gemini-4-argon",
    "opencode-go-muse-spark",
    "opencode-console-muse-spark",
    "opencode-go-deepseek-v4-flash",
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
