//! `plangraph fast-checks --dispatch`: the host registry floor and the dispatch report.

use std::{fs, path::Path, process::Command};

fn registry(dir: &Path, mem_floor_mb: u64) -> std::path::PathBuf {
    let doc = serde_json::json!({"version": 1, "self": "worker-a", "hosts": [{
        "id": "worker-a", "class": "worker", "aliases": [],
        "transport": "keeper", "os": if cfg!(windows) { "windows" } else { "linux" },
        "state_dir": dir.to_str().unwrap(),
        "caps": {"max_runs": 4, "max_writers": 2, "max_seats": 2,
                 "max_sessions": 5, "mem_floor_mb": mem_floor_mb},
        "warmup": {"warmup_s": 1, "ramp_mb": {}},
        "session_mb": 1, "roles": [], "repos": {"plangraph": {
            "root": dir.to_str().unwrap(), "worktrees_root": dir.join("trees").to_str().unwrap(),
            "remote": "example", "import_roots": [], "cache_env": {}}}
    }]});
    let path = dir.join("hosts.json");
    fs::write(&path, doc.to_string()).unwrap();
    path
}

fn dispatch(dir: &Path, floor: u64, command: &[&str]) -> std::process::Output {
    let hosts = registry(dir, floor);
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["fast-checks", "--dispatch", "--registry"])
        .arg(&hosts)
        .args(["--repo", "plangraph", "--log"])
        .arg(dir.join("dispatch.log"))
        .arg("--")
        .args(command)
        .output()
        .unwrap()
}

#[test]
fn dispatch_report_counts_busy_attempts_apart_from_test_failures() {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("dispatch.log");
    fs::write(
        &log,
        concat!(
            "{\"host\":\"a\",\"attempt\":1,\"verdict\":\"host_busy\"}\n",
            "{\"host\":\"b\",\"attempt\":2,\"verdict\":\"pass\"}\n",
            "{\"host\":\"a\",\"attempt\":1,\"verdict\":\"test_failure\"}\n",
        ),
    )
    .unwrap();
    let report = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(["fast-checks", "--dispatch", "--report"])
        .arg(&log)
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8(report.stdout).unwrap().trim(),
        r#"{"passed":1,"host_busy":1,"test_failure":1,"ignored":0}"#
    );
}

#[test]
fn dispatch_refuses_a_host_below_its_memory_floor() {
    let dir = tempfile::tempdir().unwrap();
    let result = dispatch(dir.path(), u64::MAX, &["true"]);
    assert!(!result.status.success());
    let stderr = String::from_utf8_lossy(&result.stderr);
    assert!(stderr.contains("worker-a: MemAvailable"), "{stderr}");
    assert!(!dir.path().join("dispatch.log").exists());
}
