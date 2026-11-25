//! The `--parent` launch option reaches the run record as `parent_session` under the
//! same name rule as the Python twin `annotate_native` (spawner/notify.py).
use super::sandbox::{Launcher, Sandbox};
use serde_json::{json, Value};
use std::path::Path;

/// Launches a writer with `--parent <parent>` and returns its fleet-session run record.
fn launched_record(launcher: &Launcher, parent: &str) -> Value {
    let s = Sandbox::new(launcher);
    let out = s
        .launch("writer")
        .args(["--parent", parent])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let spec = s.json("state/launch/sessions/writer-0001.json");
    let id = spec["run_id"].as_str().expect("fleet run ID");
    s.json(&format!("xdg/plangraph/runs/{id}/record.json"))
}

/// The record fragment `annotate_native` writes for each name: `{}` when it writes no key,
/// `{"parent_session": …}` when it writes one (a stored null stays visible).
fn python_parents(names: &[&str]) -> Vec<Value> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let out = crate::test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args([
            "-c",
            r#"
import json, sys
from plangraph.spawner.notify import annotate_native
rows = []
for name in json.loads(sys.argv[1]):
    record = {}
    annotate_native(record, {'run_kind': 'native-codex', 'parent_session': name})
    rows.append({k: v for k, v in record.items() if k == 'parent_session'})
print(json.dumps(rows))
"#,
        ])
        .arg(json!(names).to_string())
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}

/// Both implementations store `names[i]` as given, or write no `parent_session` key at all.
fn agree(launcher: &Launcher, names: &[&str], stored: bool) {
    let python = python_parents(names);
    for (name, python) in names.iter().zip(python) {
        let record = launched_record(launcher, name);
        let expected = if stored {
            json!({"parent_session": name})
        } else {
            json!({})
        };
        assert_eq!(python, expected, "Python for {name:?}");
        let written: serde_json::Map<String, Value> = record
            .as_object()
            .unwrap()
            .iter()
            .filter(|(key, _)| *key == "parent_session")
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        assert_eq!(
            Value::Object(written),
            expected,
            "Rust for {name:?}: {record}"
        );
    }
}

pub fn parent_session_is_recorded(launcher: &Launcher) {
    let longest = "a".repeat(128);
    agree(launcher, &["plan-order-0706", "a._-9", &longest], true);
}

pub fn invalid_parent_is_not_recorded(launcher: &Launcher) {
    let too_long = "a".repeat(129);
    let invalid = [
        "-leading-dash",
        "has space",
        "semi;colon",
        too_long.as_str(),
        "éa",
        "aé",
    ];
    agree(launcher, &invalid, false);
}
