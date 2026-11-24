//! Existing Python boundary compatibility, driven by Rust tests and fake input;
//! no network, keeper mutation, spawner process or new Python source file.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use plangraph_cli::feeder::*;
use serde_json::{json, Value};
use std::io::Write;
use std::process::Stdio;

#[test]
fn remote_ps_preserves_writer_identity_through_its_real_privacy_filter() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let mut child = test_python::command()
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
        "status":"running","item_key":"plangraph:PATCH-OTHER","started":1000,"stderr":"not public",
        "run_kind":"fleet-session"}]});
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
    // The kind survives the projection: the feeder splits a whole-card item_key by it.
    assert_eq!(rows[0]["run_kind"], "fleet-session");
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
    // The waiting record is saved through the service: the double keeps what it was asked to save.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let out=test_python::command().current_dir(root).env("PYTHONPATH",root.join("src"))
        .args(["-c",r#"import json, os
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner import seat_spawn, overflow, host_ready
saved,store=[],{}
def save(record):
    saved.append(dict(record)); store[record['run_id']]=dict(record)
service=SimpleNamespace(runner=SimpleNamespace(default_cwd='.'),_save=save,result=lambda run_id: store[run_id])
os.environ.update(PLANGRAPH_ADMISSION_WAIT_S='0.5',PLANGRAPH_ADMISSION_POLL_S='0.01')
with patch.object(overflow,'registry_caps',return_value={'self':'dell','hosts':[]}), patch.object(overflow,'placement_policy',return_value=('dell',False)), patch.object(overflow,'local_limits',return_value=(3000,2048)), patch.object(seat_spawn,'resources',return_value=(0,100)), patch.object(host_ready,'local_disk_admission',return_value=(None,None)), patch.object(overflow,'ready_records',return_value={}), patch.object(overflow,'repos_public',return_value=False), patch.object(overflow,'decide',return_value=overflow.Decision(None,'low memory')):
    final=seat_spawn.admit(service,{'role':'implement'},card={'item_key':'plangraph:QUEUE-TEST','repo':'.'})
    print(json.dumps(dict(saved[0],final=final)))
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
    assert!(row["owner_pid"].is_number());
    let end = &row["final"];
    assert_eq!(end["status"], "refused");
    assert_eq!(end["run_id"], row["run_id"]);
    assert_eq!(end["item_key"], "plangraph:QUEUE-TEST");
    assert_eq!(end["host"], "dell");
    assert_eq!(end["started"], row["started"]);
}
