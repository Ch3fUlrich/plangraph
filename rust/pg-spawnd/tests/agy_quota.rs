//! Exercise the shared routing/store boundary used by the Rust route adapter.
use serde_json::Value;
use std::process::Command;

#[test]
fn hydrogen_and_argon_limits_are_independent_and_exposed() {
    let state = tempfile::tempdir().unwrap();
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new("python3")
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("XDG_STATE_HOME", state.path())
        .env("PLANGRAPH_OMNIROUTE_LEG_STATE", "0")
        .args([
            "-c",
            r#"
import json
from plangraph.routing_support.leg_state import _default_store
from plangraph.planner.routing import plan_spawn
store = _default_store()
card = dict(estimate_tokens=1000, paths=['rust/pg-spawnd/src/spawn.rs'], has_tests=True)
def route():
    return plan_spawn(card, role='review', writer_families=['openai'],
                      leg_reader=store, catalog_rows=[]).to_dict()
initial = route()
store.record_harness_limit('agy/gemini-4-hydrogen-high',
    'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 10h40m26s.', 'fixture')
hydrogen = route()
store.record_harness_limit('agy/gemini-4-argon-medium',
    'Individual quota reached. Resets in 2h.', 'fixture')
argon = route()
store.record_harness_limit('agy/claude-sonnet-5-5-high',
    'Individual quota reached. Resets in 1h.', 'fixture')
print(json.dumps(dict(initial=initial, hydrogen=hydrogen, argon=argon, final=route())))
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
    assert_eq!(
        result["initial"]["first_leg"]["model"],
        "gemini-4-hydrogen-high"
    );
    assert_eq!(
        result["hydrogen"]["first_leg"]["model"],
        "gemini-4-argon-medium"
    );
    assert_eq!(
        result["argon"]["first_leg"]["model"],
        "console-deepseek/deepseek-flash"
    );
    let states = &result["hydrogen"]["agy_quota_states"];
    assert_eq!(states["agy:gemini-4-hydrogen"]["state"], "limited");
    assert_eq!(states["agy:gemini-4-argon"]["state"], "ok");
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
    let models: Vec<_> = result["initial"]["legs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l["model"].as_str().unwrap())
        .collect();
    assert_eq!(
        models,
        [
            "gemini-4-hydrogen-high",
            "gemini-4-argon-medium",
            "console-deepseek/deepseek-flash",
            "opencode-go/beam-free",
            "claude-sonnet-5-5-high"
        ]
    );
}
