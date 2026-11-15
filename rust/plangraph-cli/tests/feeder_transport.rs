//! Existing Python boundary compatibility, driven by Rust tests and fake input;
//! no network, keeper mutation, spawner process or new Python source file.
use plangraph_cli::feeder::*;
use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Stdio};

#[test]
fn remote_ps_preserves_writer_identity_through_its_real_privacy_filter() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let mut child = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .current_dir(root)
        .env("PYTHONPATH", root.join("src"))
        .args([
            "-c",
            r#"import json,sys
from plangraph.spawner.remote_ps import _fetch
payload=sys.stdin.read()
print(json.dumps(_fetch({'name':'remote'},lambda *args:payload)))
"#,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let payload = json!({"runs":[{"run_id":"run-remote","host":"remote","role":"implement",
        "status":"running","item_key":"plangraph:PATCH-OTHER","started":1000,"stderr":"not public"}]});
    child
        .stdin
        .take()
        .unwrap()
        .write_all(payload.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let rows: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rows[0]["item_key"], "plangraph:PATCH-OTHER");
    assert!(rows[0].get("stderr").is_none());
    let runs: Vec<Run> = serde_json::from_value(rows).unwrap();
    let ready = json!({"items":[{"key":"plangraph:PATCH-A","priority":9},{"key":"plangraph:BUILD-A","priority":1}]});
    assert_eq!(
        pick(&ready, &runs, &Policy::default()).unwrap().key,
        "plangraph:BUILD-A"
    );
}

#[test]
fn host_query_diagnostics_are_not_anonymous_writer_records() {
    let rows: Vec<Run> = serde_json::from_value(json!([
        {"host":"remote","status":"unknown","reason":"host unreachable"}
    ]))
    .unwrap();
    assert!(rows[0].diagnostic());
    assert!(!rows[0].writer());
    let ready = json!({"items":[{"key":"plangraph:A"}]});
    assert!(pick(&ready, &rows, &Policy::default()).is_some());
    let actual = Run {
        run_id: "run-unknown".into(),
        role: "implement".into(),
        status: "running".into(),
        ..Run::default()
    };
    assert!(actual.writer());
    assert!(pick(&ready, &[actual], &Policy::default()).is_some());
}

#[test]
fn feeder_unit_keeps_asynchronous_children_after_oneshot_exit() {
    let unit = include_str!("../../../deploy/systemd/pg-feeder.service");
    assert!(unit.contains("Type=oneshot"));
    assert!(unit.contains("KillMode=process"));
    assert!(unit.contains("feed --once"));
}

#[test]
fn early_admission_queue_retains_identity_host_role_and_start_time() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let out=Command::new(if cfg!(windows){"python"}else{"python3"}).current_dir(root).env("PYTHONPATH",root.join("src"))
        .args(["-c",r#"import json
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner import seat_spawn, overflow, host_ready
service=SimpleNamespace(runner=SimpleNamespace(default_cwd='.'))
with patch.object(overflow,'registry_caps',return_value={'self':'dell','hosts':[]}), patch.object(overflow,'placement_policy',return_value=('dell',False)), patch.object(overflow,'local_limits',return_value=(3000,2048)), patch.object(seat_spawn,'resources',return_value=(0,100)), patch.object(host_ready,'local_disk_refusal',return_value=None), patch.object(overflow,'ready_records',return_value={}), patch.object(overflow,'repos_public',return_value=False), patch.object(overflow,'decide',return_value=overflow.Decision(None,'low memory')):
    print(json.dumps(seat_spawn.admit(service,{'role':'implement'},card={'item_key':'plangraph:QUEUE-TEST','repo':'.'})))
"#]).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let row: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(row["status"], "queued");
    assert_eq!(row["item_key"], "plangraph:QUEUE-TEST");
    assert_eq!(row["host"], "dell");
    assert_eq!(row["role"], "implement");
    assert!(row["started"].is_number());
}
