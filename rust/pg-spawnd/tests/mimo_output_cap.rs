//! Exercise the Python compatibility boundary with Rust-owned assertions.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use serde_json::{json, Value};
use std::io::Write;
use std::process::Stdio;

const PACKAGE: &str = "@opencode/ai/providers/openai-compatible";
const FLASH: &str = "mimo/mimo-v2.6-flash";
const PRO: &str = "mimo/mimo-v2.6-pro";

fn python(body: &str, input: Value) -> Value {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let home = tempfile::tempdir().unwrap();
    let mut command = test_python::command();
    command
        .current_dir(&root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap())
        .env("HOME", home.path())
        .env("USERPROFILE", home.path())
        .env("TMPDIR", home.path())
        .env("TMP", home.path())
        .env("TEMP", home.path())
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", body])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Retain only Windows process essentials, never caller provider settings.
    for name in ["SystemRoot", "COMSPEC", "PATHEXT"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    let mut child = command.spawn().unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

const CAP: &str = r#"
import json, sys
from plangraph.spawner.opencode_answer import cap_reasoning
from plangraph.routing_errors import SpawnRefused
from plangraph.catalog_data import SEED_MODELS
request = json.load(sys.stdin)
if 'catalog_output' in request:
    for row in SEED_MODELS:
        if row['model_id'] == request['model']:
            row['max_output_tokens'] = request['catalog_output']
env = {'OPENCODE_CONFIG_CONTENT': request.get('raw', json.dumps(request.get('config'))),
       'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX': '17', 'unchanged': 'sentinel'}
before = dict(env)
try:
    ok = cap_reasoning(env, request['model'], refuse=request.get('refuse', True))
    if request.get('repeat'):
        ok = cap_reasoning(env, request['model'], refuse=True)
    reason = None
except SpawnRefused as exc:
    ok, reason = False, str(exc)
print(json.dumps(dict(ok=ok, reason=reason, env=env, before=before)))
"#;

fn config() -> Value {
    json!({"providers": {
        "mimo": {"package": PACKAGE, "body": {"provider_option": 1}, "models": {
            "mimo-v2.6-flash": {"modelID": FLASH, "limit": {"context": 1000000, "output": 32}, "body": {"custom": "keep"}},
            "mimo-v2.6-pro": {"modelID": PRO, "limit": {"context": 1000000, "output": 64}}
        }},
        "other": {"models": {"untouched": {"limit": {"output": 123}}}}
    }})
}

#[test]
fn supported_models_receive_catalog_output_in_their_own_compatible_entry() {
    for model in [FLASH, PRO] {
        for model_package in [false, true] {
            let mut before = config();
            let key = model.split_once('/').unwrap().1;
            if model_package {
                before["providers"]["mimo"]["package"] = json!("other-package");
                before["providers"]["mimo"]["models"][key]["package"] = json!(PACKAGE);
            }
            let result = python(CAP, json!({"model": model, "config": before}));
            assert_eq!(result["ok"], true, "{result}");
            assert!(result["reason"].is_null(), "{result}");
            let mut expected = before;
            expected["providers"]["mimo"]["models"][key]["limit"]["output"] = json!(131072);
            let rendered: Value =
                serde_json::from_str(result["env"]["OPENCODE_CONFIG_CONTENT"].as_str().unwrap())
                    .unwrap();
            assert_eq!(rendered, expected);
            assert_eq!(
                result["env"]["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"],
                "131072"
            );
        }
    }
}

#[test]
fn missing_or_incompatible_models_refuse_by_name_without_fabrication() {
    let mut cases = vec![("unsupported_mimo_model", "mimo/unknown", config())];
    for (field, value, reason) in [
        ("models", json!({}), "missing_model"),
        (
            "package",
            json!("@opencode/ai/providers/anthropic"),
            "non_openai_compatible",
        ),
        ("package", json!(17), "non_openai_compatible"),
    ] {
        let mut cfg = config();
        cfg["providers"]["mimo"][field] = value;
        cases.push((reason, FLASH, cfg));
    }
    cases.push(("missing_provider", FLASH, json!({"providers": {}})));
    let mut cfg = config();
    cfg["providers"]["mimo"]["models"]["mimo-v2.6-flash"]["package"] = Value::Null;
    cases.push(("non_openai_compatible", FLASH, cfg));
    for (reason, model, cfg) in cases {
        let result = python(CAP, json!({"model": model, "config": cfg}));
        assert_eq!(result["ok"], false, "{result}");
        assert_eq!(
            result["reason"],
            format!("reasoning_output_limit: {reason}")
        );
        assert_eq!(result["env"], result["before"], "refusal changed config");
    }
}

#[test]
fn malformed_limit_body_and_config_refuse_by_name_instead_of_raising() {
    for field in ["limit", "body"] {
        for value in [
            Value::Null,
            json!([]),
            json!("invalid"),
            json!(17),
            json!(false),
        ] {
            let mut cfg = config();
            cfg["providers"]["mimo"]["models"]["mimo-v2.6-flash"][field] = value;
            let result = python(CAP, json!({"model": FLASH, "config": cfg}));
            assert_eq!(
                result["reason"],
                format!("reasoning_output_limit: malformed_{field}")
            );
            assert_eq!(result["env"], result["before"]);
        }
    }
    for raw in ["{", "null", "[]"] {
        let result = python(CAP, json!({"model": FLASH, "raw": raw}));
        assert_eq!(result["reason"], "reasoning_output_limit: malformed_config");
        assert_eq!(result["env"], result["before"]);
    }
}

#[test]
fn malformed_provider_and_model_maps_refuse_without_partial_environment_writes() {
    for (cfg, reason) in [
        (json!({}), "missing_providers"),
        (json!({"provider": {"mimo": {}}}), "missing_providers"),
        (json!({"providers": []}), "malformed_providers"),
        (json!({"providers": {"mimo": null}}), "malformed_provider"),
        (
            json!({"providers": {"mimo": {"models": []}}}),
            "malformed_models",
        ),
        (
            json!({"providers": {"mimo": {"models": {"mimo-v2.6-flash": null}}}}),
            "malformed_model",
        ),
    ] {
        let result = python(CAP, json!({"model": FLASH, "config": cfg}));
        assert_eq!(
            result["reason"],
            format!("reasoning_output_limit: {reason}")
        );
        assert_eq!(result["env"], result["before"]);
        let probe = python(CAP, json!({"model": FLASH, "config": cfg, "refuse": false}));
        assert_eq!(probe["ok"], false);
        assert_eq!(probe["env"], probe["before"]);
    }
}

#[test]
fn catalog_maximum_must_be_a_positive_integer() {
    for maximum in [
        Value::Null,
        json!(true),
        json!(0),
        json!(-1),
        json!(1.5),
        json!("131072"),
    ] {
        let result = python(
            CAP,
            json!({"model": FLASH, "config": config(), "catalog_output": maximum}),
        );
        assert_eq!(
            result["reason"],
            "reasoning_output_limit: invalid_catalog_output"
        );
        assert_eq!(result["env"], result["before"]);
    }
}

#[test]
fn output_cap_is_idempotent_and_does_not_refuse_already_disabled_thinking() {
    let mut cfg = config();
    cfg["providers"]["mimo"]["models"]["mimo-v2.6-flash"] =
        json!({"body": {"thinking": {"type": "disabled"}}});
    let result = python(CAP, json!({"model": FLASH, "config": cfg, "repeat": true}));
    assert_eq!(result["ok"], true);
    let rendered: Value =
        serde_json::from_str(result["env"]["OPENCODE_CONFIG_CONTENT"].as_str().unwrap()).unwrap();
    cfg["providers"]["mimo"]["models"]["mimo-v2.6-flash"]["limit"] = json!({"output": 131072});
    assert_eq!(rendered, cfg);
}

const LAUNCH: &str = r#"
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.runner import SpawnerRunner
request = json.load(sys.stdin)
root = Path(os.environ['HOME'])
providers = root / 'providers.json'
providers.write_text('{"providers": {}}')
os.environ['PLANGRAPH_PROVIDERS_FILE'] = str(providers)
os.environ['OMNIROUTE_BASE_URL'] = 'https://gateway.invalid/v1'
rows = [dict(id=request.get('resolved', request['model']), combos=['review-combo'],
             active=True, available=1)]
calls = []
async def ready(*args, **kwargs): return None
async def collect(*args): return b'', b''
async def factory(*args, **kwargs):
    calls.append(kwargs['env'])
    return NS(pid=None, returncode=0)
async def run():
    runner = SpawnerRunner(default_cwd=str(root), process_factory=factory)
    runner._reap_process = ready
    runner._init_git_config_guard = lambda *args: None
    try:
        job = await runner.spawn('Review', model_or_combo=request['model'],
            role=request.get('role', 'review'), reasoning_cap=request.get('reasoning_cap', False),
            wait_seconds=2, extra_env={
                'OPENCODE_CONFIG_CONTENT': json.dumps(request['config']),
                'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX': '17'})
        return dict(status=job.status, reason=job.stderr, events=job.events, calls=calls,
                    model=job.model_or_combo, retained=bool(runner._ocdata_dirs))
    except SpawnRefused as exc:
        return dict(status='refused', reason=str(exc), calls=calls)
with patch('plangraph.spawner.resolver.get_catalog_models', return_value=rows), \
     patch('plangraph.spawner.resolver.host_skip_reason_async', ready), \
     patch('plangraph.spawner.host_ready.cargo_child_env', side_effect=lambda env, *, pin_configured, target=None: env), \
     patch('plangraph.spawner.host_ready.cargo_lease', return_value=None), \
     patch('plangraph.spawner.opencode_isolation.record_baseline'), \
     patch('plangraph.spawner.gateway_witness.witness'), \
     patch('plangraph.spawner.runner._opencode_served_model', return_value=None), \
     patch('plangraph.spawner.host_ready.collect_with_disk_guard', collect):
    result = asyncio.run(run())
print(json.dumps(result))
"#;

#[test]
fn launch_gate_caps_every_supported_mimo_role_and_resolved_combo_without_refusal() {
    for model in [FLASH, PRO, "review-combo"] {
        for role in ["review", "implement", "orchestrate"] {
            let result = python(
                LAUNCH,
                json!({"model": model, "resolved": FLASH, "role": role, "config": config()}),
            );
            assert_eq!(result["status"], "completed", "{result}");
            assert_eq!(result["calls"].as_array().unwrap().len(), 1, "{result}");
            let env = &result["calls"][0];
            let cfg: Value =
                serde_json::from_str(env["OPENCODE_CONFIG_CONTENT"].as_str().unwrap()).unwrap();
            let key = result["model"].as_str().unwrap().split_once('/').unwrap().1;
            assert_eq!(
                cfg["providers"]["mimo"]["models"][key]["limit"]["output"],
                131072
            );
            assert_eq!(env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], "131072");
            assert_eq!(result["retained"], false);
            if role == "review" {
                assert!(result["events"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|e| e["event"] == "opencode_tools_denied"));
            }
        }
    }
}

#[test]
fn launch_refuses_unknown_combo_incompatible_package_and_malformed_limit_before_process() {
    for (model, resolved, field, value, reason) in [
        (
            "mimo/unknown",
            "mimo/unknown",
            "body",
            json!({}),
            "Mimo requires one of:",
        ),
        (
            "review-combo",
            "mimo/unknown",
            "body",
            json!({}),
            "Mimo requires one of:",
        ),
        (
            FLASH,
            FLASH,
            "limit",
            Value::Null,
            "reasoning_output_limit: malformed_limit",
        ),
        (
            FLASH,
            FLASH,
            "body",
            json!([]),
            "reasoning_output_limit: malformed_body",
        ),
        (
            FLASH,
            FLASH,
            "package",
            json!("wrong"),
            "reasoning_output_limit: non_openai_compatible",
        ),
    ] {
        let mut cfg = config();
        cfg["providers"]["mimo"]["models"]["mimo-v2.6-flash"][field] = value;
        let result = python(
            LAUNCH,
            json!({"model": model, "resolved": resolved, "config": cfg}),
        );
        assert!(result["calls"].as_array().unwrap().is_empty(), "{result}");
        assert!(
            result["reason"].as_str().unwrap().starts_with(reason),
            "{result}"
        );
    }
}

#[test]
fn non_mimo_launch_preserves_its_model_config_and_existing_review_env_filter() {
    let before = json!({"providers": {"omniroute": {"models": {"cx-gpt-6-luna": {
        "limit": {"output": 456}, "body": {"custom": "keep"}
    }}}}});
    for (role, reasoning_cap) in [
        ("review", false),
        ("implement", false),
        ("orchestrate", false),
        ("implement", true),
    ] {
        let result = python(
            LAUNCH,
            json!({"model": "omniroute/cx-gpt-6-luna", "config": before, "role": role, "reasoning_cap": reasoning_cap}),
        );
        assert_eq!(result["status"], "completed", "{result}");
        let env = &result["calls"][0];
        let cfg: Value =
            serde_json::from_str(env["OPENCODE_CONFIG_CONTENT"].as_str().unwrap()).unwrap();
        assert_eq!(
            cfg["providers"]["omniroute"]["models"],
            before["providers"]["omniroute"]["models"]
        );
        if role == "review" {
            assert!(env.get("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX").is_none());
        } else {
            assert_eq!(env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], "17");
        }
    }
}

#[test]
fn redaction_keeps_only_nonnegative_integer_usage_not_token_secrets() {
    let result = python(
        r#"
import json, sys
from plangraph.spawner.env_redact import redact
print(json.dumps(redact(json.load(sys.stdin))))
"#,
        json!({"output_tokens": 0, "reasoning_tokens": 32002,
            "nested": [{"output_tokens": "secret", "reasoning_tokens": true},
                       {"output_tokens": -1, "reasoning_tokens": null}], "api_token": 123}),
    );
    assert_eq!(
        result,
        json!({"output_tokens": 0, "reasoning_tokens": 32002,
        "nested": [{"output_tokens": "[REDACTED]", "reasoning_tokens": "[REDACTED]"},
                   {"output_tokens": "[REDACTED]", "reasoning_tokens": "[REDACTED]"}],
        "api_token": "[REDACTED]"})
    );
}

#[test]
fn seat_launch_spec_receives_the_same_cap_and_denied_tool_evidence() {
    let result = python(
        r#"
import json, os, sys
from plangraph.spawner.launch_spec import build
from pathlib import Path
request = json.load(sys.stdin)
root = Path(os.environ['HOME'])
providers = root / 'providers.json'
providers.write_text('{"providers": {}}')
os.environ['PLANGRAPH_PROVIDERS_FILE'] = str(providers)
os.environ['OMNIROUTE_BASE_URL'] = 'https://gateway.invalid/v1'
os.environ['OPENCODE_CONFIG_CONTENT'] = json.dumps(request['config'])
result = build(dict(run_id='run-' + 'a' * 32, job_id='fixture', role='review', dry_run=True, writer_families=['gpt'],
    leg=dict(id='mimo-v2.6-flash', explicit=True), prompt='Review', card={},
    cwd=str(root), state=str(root / 'state')))
print(json.dumps(result))
"#,
        json!({"config": config()}),
    );
    let env = &result["env"];
    let cfg: Value =
        serde_json::from_str(env["OPENCODE_CONFIG_CONTENT"].as_str().unwrap()).unwrap();
    assert_eq!(
        cfg["providers"]["mimo"]["models"]["mimo-v2.6-flash"]["limit"]["output"],
        131072
    );
    assert_eq!(env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], "131072");
    assert!(result["events"]
        .as_array()
        .unwrap()
        .iter()
        .any(|e| e["event"] == "opencode_tools_denied"));
}

#[test]
fn mimo_cards_and_launches_carry_reasoning_cap_below_output_limit() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let providers: Value = serde_json::from_str(
        &std::fs::read_to_string(root.join("configuration/opencode/providers.json")).unwrap(),
    )
    .unwrap();
    for key in ["mimo-v2.6-flash", "mimo-v2.6-pro"] {
        let card = &providers["providers"]["mimo"]["models"][key];
        let output = card["limit"]["output"].as_u64().unwrap();
        let reasoning = card["body"]["reasoning"]["max_tokens"].as_u64().unwrap();
        assert_eq!(output, 131072, "{key}");
        assert!(
            reasoning > 0 && reasoning < output,
            "{key}: {reasoning} vs {output}"
        );
    }
    let rendered = python(
        r#"
import json, sys
from plangraph.routing_support.opencode_render import render_gateway_providers
req = json.load(sys.stdin)
env = {"OMNIROUTE_BASE_URL": "https://gateway.invalid/v1", "OPENCODE_CONFIG_CONTENT": "{}"}
assert render_gateway_providers(env, req["model"]) is None
print(env["OPENCODE_CONFIG_CONTENT"])
"#,
        json!({"model": FLASH}),
    );
    for key in ["mimo-v2.6-flash", "mimo-v2.6-pro"] {
        let card = &rendered["providers"]["mimo"]["models"][key];
        let output = card["limit"]["output"].as_u64().unwrap();
        let reasoning = card["body"]["reasoning"]["max_tokens"].as_u64().unwrap();
        assert_eq!(output, 65536);
        assert!(reasoning > 0 && reasoning < output);
    }
    let launch = python(
        LAUNCH,
        json!({"model": FLASH, "resolved": FLASH, "role": "review", "config": config()}),
    );
    let cfg: Value = serde_json::from_str(
        launch["calls"][0]["OPENCODE_CONFIG_CONTENT"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    let card = &cfg["providers"]["mimo"]["models"]["mimo-v2.6-flash"];
    let output = card["limit"]["output"].as_u64().unwrap();
    let reasoning = card["body"]["reasoning"]["max_tokens"].as_u64().unwrap();
    assert!(reasoning > 0 && reasoning < output);
}
