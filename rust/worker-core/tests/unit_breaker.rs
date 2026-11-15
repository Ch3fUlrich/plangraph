#![cfg(unix)]
use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};

fn run(root: &Path, args: &[&str]) -> std::process::Output {
    // Fail at runtime on main, where Cargo has no executable to expose yet.
    let binary = match option_env!("CARGO_BIN_EXE_unit-breaker") {
        Some(binary) => binary,
        None => panic!("plangraph must ship unit-breaker"),
    };
    Command::new(binary)
        .args(args)
        .env("XDG_STATE_HOME", root)
        .env("AUTOOS_ALERT_INBOX", root.join("routing-alerts"))
        .output()
        .unwrap()
}
fn state(root: &Path, age: i64) -> std::path::PathBuf {
    let dir = root.join("autoos/breaker");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("ctx-watchdog.timer.json");
    fs::write(&path, json!({"tripped":true,"alerted":true,"count":5,"sig":"io",
        "updated":(chrono::Utc::now()-chrono::Duration::seconds(age)).format("%Y-%m-%dT%H:%M:%SZ").to_string()}).to_string()).unwrap();
    path
}
const OK: &[&str] = &["--unit", "ctx-watchdog.timer", "--", "/bin/true"];
const FAIL: &[&str] = &[
    "--unit",
    "ctx-watchdog.timer",
    "--threshold",
    "1",
    "--",
    "/bin/false",
];

#[test]
fn legacy_trip_half_opens_after_fifteen_minutes_and_success_resets() {
    let d = tempfile::tempdir().unwrap();
    let p = state(d.path(), 901);
    fs::write(p.with_extension("tripped"), "legacy marker").unwrap();
    assert!(run(d.path(), OK).status.success());
    let value: Value = serde_json::from_slice(&fs::read(p).unwrap()).unwrap();
    assert_eq!(value["count"], 0);
    assert_ne!(value["tripped"], true);
    assert!(!d
        .path()
        .join("autoos/breaker/ctx-watchdog.timer.tripped")
        .exists());
    assert!(run(d.path(), &["status"]).stdout.is_empty());
}

#[test]
fn fresh_trip_blocks_child_then_failed_half_open_renews_cooldown() {
    let d = tempfile::tempdir().unwrap();
    let p = state(d.path(), 0);
    let marker = d.path().join("child-ran");
    let args = [
        "--unit",
        "ctx-watchdog.timer",
        "--",
        "/usr/bin/touch",
        marker.to_str().unwrap(),
    ];
    assert!(!run(d.path(), &args).status.success());
    assert!(!marker.exists());
    state(d.path(), 901);
    // Legacy I/O trip, new "other" failure, default threshold five: a failed
    // half-open run must re-trip even though the new class has count one.
    assert_eq!(
        run(
            d.path(),
            &["--unit", "ctx-watchdog.timer", "--", "/bin/false"]
        )
        .status
        .code(),
        Some(1)
    );
    assert!(!run(d.path(), &args).status.success());
    assert!(!marker.exists());
    let value: Value = serde_json::from_slice(&fs::read(p).unwrap()).unwrap();
    assert_eq!(value["tripped"], true);
}

#[test]
fn trip_publishes_needs_you_and_routing_alert_without_disabling_timer() {
    let d = tempfile::tempdir().unwrap();
    assert_eq!(run(d.path(), FAIL).status.code(), Some(1));
    let alert = fs::read_to_string(d.path().join("routing-alerts")).unwrap();
    assert!(alert.contains("BREAKER ctx-watchdog.timer tripped"));
    let item = d
        .path()
        .join("autoos/breaker/needs-you/ctx-watchdog.timer.json");
    let value: Value = serde_json::from_slice(&fs::read(&item).unwrap()).unwrap();
    assert_eq!(value["owner"], "routing");
    assert_eq!(value["kind"], "needs-you");
    assert!(!run(d.path(), FAIL).status.success());
    assert_eq!(
        fs::read_to_string(d.path().join("routing-alerts")).unwrap(),
        alert
    );
    let status = run(d.path(), &["status"]);
    assert!(status.status.success());
    assert!(String::from_utf8(status.stdout)
        .unwrap()
        .contains("ctx-watchdog.timer state=tripped"));
    assert!(run(d.path(), &["reset", "ctx-watchdog.timer"])
        .status
        .success());
    assert!(!item.exists());
    assert!(run(d.path(), &["status"]).stdout.is_empty());
}

#[test]
fn malformed_state_and_invalid_units_fail_closed() {
    let d = tempfile::tempdir().unwrap();
    let p = state(d.path(), 901);
    fs::write(p, "broken json").unwrap();
    assert!(!run(d.path(), OK).status.success());
    assert!(!run(d.path(), &["status"]).status.success());
    assert!(!run(d.path(), &["reset", "../escape"]).status.success());
}

#[test]
fn legacy_alert_failure_remains_pending_until_routing_line_is_written() {
    let d = tempfile::tempdir().unwrap();
    let p = state(d.path(), 0);
    fs::create_dir(d.path().join("routing-alerts")).unwrap();
    assert_eq!(run(d.path(), OK).status.code(), Some(2));
    let value: Value = serde_json::from_slice(&fs::read(&p).unwrap()).unwrap();
    assert_eq!(value["alerted"], false);
    fs::remove_dir(d.path().join("routing-alerts")).unwrap();
    assert_eq!(run(d.path(), OK).status.code(), Some(1));
    assert!(fs::read_to_string(d.path().join("routing-alerts"))
        .unwrap()
        .contains("BREAKER"));
}

#[test]
fn unit_lock_prevents_a_second_half_open_child() {
    let d = tempfile::tempdir().unwrap();
    state(d.path(), 901);
    let lock = fs::File::create(d.path().join("autoos/breaker/ctx-watchdog.timer.lock")).unwrap();
    lock.lock().unwrap();
    assert_eq!(run(d.path(), OK).status.code(), Some(2));
    drop(lock);
    assert!(run(d.path(), OK).status.success());
}

#[test]
fn launch_errors_are_counted_and_threshold_and_success_are_respected() {
    let d = tempfile::tempdir().unwrap();
    let args = [
        "--unit",
        "w.timer",
        "--threshold",
        "2",
        "--",
        "/missing-program",
    ];
    assert_eq!(run(d.path(), &args).status.code(), Some(1));
    assert!(run(d.path(), &["status"]).stdout.is_empty());
    assert!(run(d.path(), &["--unit", "w.timer", "--", "/bin/true"])
        .status
        .success());
    assert_eq!(run(d.path(), &args).status.code(), Some(1));
    assert!(run(d.path(), &["status"]).stdout.is_empty());
    assert_eq!(run(d.path(), &args).status.code(), Some(1));
    assert!(!run(d.path(), &["status"]).stdout.is_empty());
}

#[test]
fn stop_exit_codes_leave_state_unchanged() {
    let d = tempfile::tempdir().unwrap();
    let p = state(d.path(), 901);
    let original = fs::read(&p).unwrap();
    for code in ["130", "143"] {
        let result = run(
            d.path(),
            &[
                "--unit",
                "ctx-watchdog.timer",
                "--",
                "/bin/sh",
                "-c",
                &format!("exit {code}"),
            ],
        );
        assert_eq!(result.status.code().unwrap().to_string(), code);
        assert_eq!(fs::read(&p).unwrap(), original);
    }
}
