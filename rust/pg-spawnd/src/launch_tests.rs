use crate::launch::*;
use serde_json::{json, Value};
use std::{
    os::unix::fs::PermissionsExt,
    time::{Duration, Instant},
};

fn fixture() -> (std::path::PathBuf, Value) {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&dir).unwrap();
    let spec = json!({"argv":["sh"],"cwd":dir,"env":{},"stdin":null});
    (dir, spec)
}
fn script(dir: &std::path::Path, body: &str) -> LaunchAdapter {
    let path = dir.join("launch");
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    LaunchAdapter(Some(path.into_os_string()))
}
#[test]
fn contract_table() {
    let (dir, valid) = fixture();
    for (key, value) in [
        ("argv", json!([])),
        ("argv", json!([1])),
        ("argv", json!(["-sh"])),
        ("argv", json!([""])),
        ("argv", json!(["sh\0"])),
        ("argv", json!(vec!["sh"; 1025])),
        ("cwd", json!(dir.join("missing"))),
        ("cwd", json!(1)),
        ("env", json!({"X":1})),
        ("env", json!({"X=Y":"v"})),
        ("env", json!({"":"v"})),
        ("env", json!({"X\0":"v"})),
        ("env", json!({"X":"v\0"})),
        ("stdin", json!(1)),
        ("stdin", json!("x".repeat(8 * 1024 * 1024 + 1))),
        ("warnings", json!([1])),
        ("events", json!([{}])),
        ("events", json!([{"event":1}])),
    ] {
        let mut value_spec = valid.clone();
        value_spec[key] = value;
        assert!(
            matches!(parse(&serde_json::to_vec(&value_spec).unwrap()), Err(crate::errors::ToolError::Internal(s)) if s == "launch adapter returned an invalid launch spec"),
            "{key}"
        );
    }
    let mut missing = valid.clone();
    let mut large_env = valid.clone();
    large_env["env"] = Value::Object(
        (0..1025)
            .map(|n| (format!("KEY{n}"), json!("value")))
            .collect(),
    );
    assert!(parse(&serde_json::to_vec(&large_env).unwrap()).is_err());
    missing.as_object_mut().unwrap().remove("stdin");
    assert!(parse(&serde_json::to_vec(&missing).unwrap()).is_err());
    let mut good = valid;
    good["warnings"] = json!(["warning"]);
    good["events"] = json!([{"event":"ready","extra":true}]);
    good["unknown"] = json!(true);
    let spec = parse(&serde_json::to_vec(&good).unwrap()).ok().unwrap();
    assert_eq!(spec.warnings.unwrap(), ["warning"]);
    assert_eq!(spec.events.unwrap()[0]["event"], "ready");
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn request_pipe_and_exits() {
    let (dir, valid) = fixture();
    std::fs::write(dir.join("spec"), serde_json::to_vec(&valid).unwrap()).unwrap();
    let adapter = script(
        &dir,
        &format!("cat > '{0}/request'\ncat '{0}/spec'", dir.display()),
    );
    let request = json!({"run_id":"run","job_id":"job","role":"implement","dry_run":false,"leg":{},"prompt":" verbatim\n","card":{},"cwd":dir,"state":dir});
    assert!(adapter
        .request(&dir, &request, Duration::from_secs(1))
        .is_ok());
    let saved: Value =
        serde_json::from_slice(&std::fs::read(dir.join("request")).unwrap()).unwrap();
    assert_eq!(saved, request);
    assert_eq!(saved.as_object().unwrap().len(), 9);
    for (body, expected) in [
        (
            "echo 'SpawnRefused: nope' >&2; exit 2",
            "SpawnRefused: nope",
        ),
        ("exit 1", "launch adapter failed (exit 1)"),
        (
            "echo broken",
            "launch adapter returned an invalid launch spec",
        ),
    ] {
        let error = script(&dir, body)
            .request(&dir, &request, Duration::from_secs(1))
            .err()
            .unwrap()
            .into_error_data();
        assert_eq!(error.message, expected);
    }
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn timeout_kills_group() {
    let (dir, _) = fixture();
    let adapter = script(
        &dir,
        &format!("echo $$ > '{}/pid'\nsleep 30 &\nwait", dir.display()),
    );
    let start = Instant::now();
    let error = adapter
        .request(&dir, &json!({}), Duration::from_millis(300))
        .err()
        .unwrap()
        .into_error_data();
    assert_eq!(error.message, "launch adapter timed out");
    assert!(start.elapsed() < Duration::from_secs(2));
    let pid: i32 = std::fs::read_to_string(dir.join("pid"))
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
    std::fs::remove_dir_all(dir).unwrap();
}
