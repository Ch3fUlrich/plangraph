use std::{fs, process::Command};

#[test]
fn reports_old_finished_jobs_without_removing_them() {
    let jobs = tempfile::tempdir().unwrap();
    for (name, state, updated) in [
        ("old-done", "done", "2000-01-01T00:00:00Z"),
        ("old-idle", "idle", "2000-01-01T00:00:00Z"),
        ("recent-done", "done", "2999-01-01T00:00:00Z"),
        ("unknown-age", "done", "2000-01-01"),
    ] {
        let dir = jobs.path().join(name);
        fs::create_dir(&dir).unwrap();
        fs::write(
            dir.join("state.json"),
            serde_json::json!({"state":state,"updatedAt":updated}).to_string(),
        )
        .unwrap();
        let old = std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(946_684_800);
        for path in [dir.join("state.json"), dir] {
            fs::File::open(path).unwrap().set_modified(old).unwrap();
        }
    }
    let output = Command::new(env!("CARGO_BIN_EXE_pg-worktree-sweep"))
        .arg("--report-claude-jobs")
        .arg(jobs.path())
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let rows: Vec<serde_json::Value> = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(
        rows[0]["path"],
        jobs.path().join("old-done").to_str().unwrap()
    );
    assert_eq!(fs::read_dir(jobs.path()).unwrap().count(), 4);
}
