#![cfg(feature = "test-support")]
use pg_worker_core::live_probe::*;
use serde_json::json;

fn spec() -> Spec {
    serde_json::from_value(json!({"command":child("witness"),"expected":"witness\n"})).unwrap()
}

fn child(mode: &str) -> Vec<String> {
    vec![env!("CARGO_BIN_EXE_live_probe_child").into(), mode.into()]
}

#[test]
fn feature_requires_fresh_matching_latest_pass() {
    let s = spec();
    let pass = run("p:F", &s, 100).unwrap();
    assert!(close_gate("feature", "p:F", Some(&s), std::slice::from_ref(&pass), 101).is_ok());
    assert!(close_gate("feature", "p:F", None, &[], 101).is_err());
    assert!(close_gate("feature", "p:G", Some(&s), std::slice::from_ref(&pass), 101).is_err());
    assert!(close_gate(
        "feature",
        "p:F",
        Some(&s),
        std::slice::from_ref(&pass),
        86501
    )
    .is_err());
    assert!(close_gate("feature", "p:F", Some(&s), std::slice::from_ref(&pass), 99).is_err());
    let mut changed = s.clone();
    changed.expected = "different".into();
    assert!(close_gate(
        "feature",
        "p:F",
        Some(&changed),
        std::slice::from_ref(&pass),
        101
    )
    .is_err());
    let mut failed = pass.clone();
    failed.passed = false;
    failed.at = 101;
    assert!(close_gate("feature", "p:F", Some(&s), &[pass, failed], 102).is_err());
    assert!(close_gate("bug", "p:F", None, &[], 102).is_ok());
}

#[test]
fn runner_checks_exit_output_and_timeout() {
    let mut s = spec();
    assert!(std::path::Path::new(&s.command[0]).is_absolute());
    assert!(run("p:F", &s, 1).unwrap().passed);
    s.expected = "wrong".into();
    assert!(!run("p:F", &s, 1).unwrap().passed);
    s.command = child("fail");
    s.expected.clear();
    assert!(!run("p:F", &s, 1).unwrap().passed);
    s.command = child("sleep");
    s.timeout_secs = 1;
    assert!(!run("p:F", &s, 1).unwrap().passed);
}

#[test]
fn daily_runner_records_failure_and_gate_refuses_it() {
    let dir = tempfile::tempdir().unwrap();
    let items = dir.path().join("items.json");
    let journal = dir.path().join("results.jsonl");
    let write_items = |expected: &str| {
        std::fs::write(
            &items,
            serde_json::to_vec(&json!([{
                "key":"p:F", "kind":"feature", "live_probe":{
                    "command":child("witness"), "expected":expected
                }
            }]))
            .unwrap(),
        )
        .unwrap();
    };
    let invoke = |mode: &str| {
        std::process::Command::new(env!("CARGO_BIN_EXE_pg-live-probe"))
            .arg(mode)
            .arg(&items)
            .arg(&journal)
            .args(if mode == "gate" { vec!["p:F"] } else { vec![] })
            .output()
            .unwrap()
    };
    write_items("witness\n");
    assert!(invoke("run").status.success());
    assert!(invoke("gate").status.success());
    write_items("wrong");
    assert!(!invoke("run").status.success());
    assert!(!invoke("gate").status.success());
    let text = std::fs::read_to_string(&journal).unwrap();
    assert_eq!(text.lines().count(), 2);
    let last: ResultRecord = serde_json::from_str(text.lines().last().unwrap()).unwrap();
    assert!(!last.passed);
    std::fs::write(&journal, "broken json\n").unwrap();
    assert!(!invoke("gate").status.success());
}

#[test]
fn output_setup_failure_invalidates_prior_pass() {
    let dir = tempfile::tempdir().unwrap();
    let items = dir.path().join("items.json");
    let journal = dir.path().join("results.jsonl");
    std::fs::write(
        &items,
        serde_json::to_vec(&json!([{
            "key":"p:F", "kind":"feature", "live_probe":spec()
        }]))
        .unwrap(),
    )
    .unwrap();
    let invoke = |mode: &str, tmp: &std::path::Path| {
        std::process::Command::new(env!("CARGO_BIN_EXE_pg-live-probe"))
            .arg(mode)
            .arg(&items)
            .arg(&journal)
            .args(if mode == "gate" { vec!["p:F"] } else { vec![] })
            .env("TMPDIR", tmp)
            .output()
            .unwrap()
    };
    assert!(invoke("run", dir.path()).status.success());
    assert!(!invoke("run", &dir.path().join("missing")).status.success());
    assert!(!invoke("gate", dir.path()).status.success());
    assert_eq!(std::fs::read_to_string(journal).unwrap().lines().count(), 2);
}

#[test]
fn freshness_boundary_and_equal_timestamp_order() {
    let s = spec();
    let pass = run("p:F", &s, 100).unwrap();
    assert!(close_gate(
        "feature",
        "p:F",
        Some(&s),
        std::slice::from_ref(&pass),
        86500
    )
    .is_ok());
    let mut fail = pass.clone();
    fail.passed = false;
    assert!(close_gate(
        "feature",
        "p:F",
        Some(&s),
        &[pass.clone(), fail.clone()],
        100
    )
    .is_err());
    assert!(close_gate("feature", "p:F", Some(&s), &[fail, pass], 100).is_ok());
}

fn gate_fixture(items: serde_json::Value, stale: bool, target: Option<&str>) -> bool {
    let dir = tempfile::tempdir().unwrap();
    let items_path = dir.path().join("items.json");
    let journal = dir.path().join("results.jsonl");
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let record = run("p:F", &spec(), if stale { now - 86401 } else { now }).unwrap();
    std::fs::write(&items_path, serde_json::to_vec(&items).unwrap()).unwrap();
    std::fs::write(&journal, serde_json::to_vec(&record).unwrap()).unwrap();
    let mut cmd = std::process::Command::new(env!("CARGO_BIN_EXE_pg-live-probe"));
    cmd.arg("gate").arg(items_path).arg(journal);
    if let Some(key) = target {
        cmd.arg(key);
    }
    cmd.output().unwrap().status.success()
}

#[test]
fn gate_refuses_empty_items() {
    assert!(!gate_fixture(json!([]), false, Some("p:F")));
    assert!(!gate_fixture(json!([]), false, None));
}

#[test]
fn gate_refuses_partial_items() {
    let items = json!([{"key":"p:G", "kind":"feature", "live_probe":spec()}]);
    assert!(!gate_fixture(items.clone(), false, Some("p:F")));
    assert!(!gate_fixture(items, false, None));
}

#[test]
fn gate_exempts_only_explicitly_listed_non_feature() {
    let items = json!([{"key":"p:B", "kind":"bug"}]);
    assert!(gate_fixture(items.clone(), false, Some("p:B")));
    assert!(!gate_fixture(items.clone(), false, Some("p:F")));
    assert!(!gate_fixture(items, false, None));
}

#[test]
fn gate_accepts_passing_target_record() {
    assert!(gate_fixture(
        json!([{"key":"p:F", "kind":"feature", "live_probe":spec()}]),
        false,
        Some("p:F")
    ));
}

#[test]
fn gate_refuses_stale_target_record() {
    assert!(!gate_fixture(
        json!([{"key":"p:F", "kind":"feature", "live_probe":spec()}]),
        true,
        Some("p:F")
    ));
}
