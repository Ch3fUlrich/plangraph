use pg_worker_core::live_probe::*;
use serde_json::json;

fn spec() -> Spec {
    serde_json::from_value(json!({"command":["/bin/echo","witness"],"expected":"witness\n"})).unwrap()
}

#[test]
fn feature_requires_fresh_matching_latest_pass() {
    let s = spec();
    let pass = run("p:F", &s, 100).unwrap();
    assert!(close_gate("feature", "p:F", Some(&s), &[pass.clone()], 101).is_ok());
    assert!(close_gate("feature", "p:F", None, &[], 101).is_err());
    assert!(close_gate("feature", "p:G", Some(&s), &[pass.clone()], 101).is_err());
    assert!(close_gate("feature", "p:F", Some(&s), &[pass.clone()], 86501).is_err());
    assert!(close_gate("feature", "p:F", Some(&s), &[pass.clone()], 99).is_err());
    let mut changed = s.clone();
    changed.expected = "different".into();
    assert!(close_gate("feature", "p:F", Some(&changed), &[pass.clone()], 101).is_err());
    let mut failed = pass.clone();
    failed.passed = false;
    failed.at = 101;
    assert!(close_gate("feature", "p:F", Some(&s), &[pass, failed], 102).is_err());
    assert!(close_gate("bug", "p:F", None, &[], 102).is_ok());
}

#[test]
fn runner_checks_exit_output_and_timeout() {
    let mut s = spec();
    assert!(run("p:F", &s, 1).unwrap().passed);
    s.expected = "wrong".into();
    assert!(!run("p:F", &s, 1).unwrap().passed);
    s.command = vec!["/bin/false".into()];
    s.expected.clear();
    assert!(!run("p:F", &s, 1).unwrap().passed);
    s.command = vec!["/bin/sleep".into(), "2".into()];
    s.timeout_secs = 1;
    assert!(!run("p:F", &s, 1).unwrap().passed);
}

#[test]
fn daily_runner_records_failure_and_gate_refuses_it() {
    let dir = tempfile::tempdir().unwrap();
    let items = dir.path().join("items.json");
    let journal = dir.path().join("results.jsonl");
    let write_items = |expected: &str| {
        std::fs::write(&items, serde_json::to_vec(&json!([{
            "key":"p:F", "kind":"feature", "live_probe":{
                "command":["/bin/echo","witness"], "expected":expected
            }
        }])).unwrap()).unwrap();
    };
    let invoke = |mode: &str| std::process::Command::new(env!("CARGO_BIN_EXE_pg-live-probe"))
        .arg(mode).arg(&items).arg(&journal).output().unwrap();
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
