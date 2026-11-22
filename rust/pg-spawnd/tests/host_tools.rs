use std::process::Command;

#[cfg(unix)]
#[test]
fn work_disk_cli_and_native_writer_name_the_unavailable_mount() {
    let temp = tempfile::tempdir().unwrap();
    let mount = temp.path().join("offline");
    let binary = env!("CARGO_BIN_EXE_pg-spawnd");
    let command = || {
        let mut cmd = Command::new(binary);
        cmd.env("PLANGRAPH_WORKTREE_MOUNTS", &mount)
            .env_remove("PLANGRAPH_DISK_MIN_FREE_PERCENT");
        cmd
    };
    let invalid = command()
        .env("PLANGRAPH_DISK_MIN_FREE_PERCENT", "abc")
        .args(["host", "work-disk"])
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8_lossy(&invalid.stderr).trim(),
        "disk_low: invalid free percentage"
    );
    let output = command().args(["host", "work-disk"]).output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    let reason = String::from_utf8(output.stderr).unwrap();
    assert!(reason.starts_with("disk_low:"));
    assert!(reason.contains(mount.to_str().unwrap()));

    let run = temp.path().join("runs/run-fixture");
    std::fs::create_dir_all(&run).unwrap();
    let record = run.join("record.json");
    let request = run.join("request.json");
    std::fs::write(
        &request,
        serde_json::json!({
            "record": {"repo": temp.path(), "run_id": "run-fixture"},
            "record_path": record,
        })
        .to_string(),
    )
    .unwrap();
    let native = command()
        .arg("native-writer")
        .arg(request)
        .output()
        .unwrap();
    assert!(native.status.success(), "{native:?}");
    let row: serde_json::Value = serde_json::from_slice(&std::fs::read(record).unwrap()).unwrap();
    assert_eq!(row["status"], "refused");
    assert_eq!(row["exit_code"], 8);
    assert_eq!(row["reason"], reason.trim());
    assert!(!run.join("worktree").exists());
}

#[test]
fn wsl_cache_guard_exposes_the_host_command_and_refuses_zero() {
    let help = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["host", "wsl-cache-guard", "--help"])
        .output()
        .unwrap();
    assert!(help.status.success(), "{help:?}");
    assert!(String::from_utf8_lossy(&help.stdout).contains("--min-free-mb"));
    let invalid = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["host", "wsl-cache-guard", "--min-free-mb", "0"])
        .output()
        .unwrap();
    assert!(!invalid.status.success(), "{invalid:?}");
    assert!(String::from_utf8_lossy(&invalid.stderr).contains("positive integer"));
}
