use super::tests::fixture;
use super::*;
use serde_json::json;
pub(super) struct TempDir(PathBuf);
impl TempDir {
    pub(super) fn new() -> Self {
        let path = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    pub(super) fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn layout_error_preserves_record_and_run_directory() {
    let (_temp, config, _, mut record) = fixture(false);
    record.set("run_id", json!("another-run"));
    enforce(&mut record, Status::Completed, json!({}));
    assert_eq!(record.get("reason"), Some(&json!("gitconfig_guard_error")));
    assert_eq!(record.get("gitconfig_changed_paths"), Some(&json!([])));
    assert!(record.path().is_file());
    assert!(record.path().parent().unwrap().is_dir());
    assert!(config.is_file());
}

#[test]
fn created_during_run_removed() {
    let (_temp, config, local, mut record) = fixture(true);
    let original = fs::read(&config).unwrap();
    assert!(!local.exists());
    fs::write(&local, b"new local\n").unwrap();
    assert_eq!(
        enforce(&mut record, Status::Completed, json!({})),
        Status::Failed
    );
    assert!(!local.exists());
    assert_eq!(fs::read(&config).unwrap(), original);
}

#[test]
fn non_checkout_noop() {
    let temp = TempDir::new();
    assert!(!capture(temp.path(), &temp.path().join("run")).unwrap());
    assert!(!temp.path().join("run").exists());
}

#[test]
fn record_api_always_supplies_a_parent() {
    let (temp, _, _, record) = fixture(false);
    let expected = run_dir(temp.path(), "run-guard")
        .unwrap()
        .join("record.json");
    assert_eq!(record.path(), expected.as_path());
    let parent = record.path().parent().unwrap();
    assert!(!parent.as_os_str().is_empty());
    assert!(parent.is_dir());
}

#[test]
fn guard_diagnostic_ignores_stale_record_values() {
    // D-4893: record-level reason/exit_code belong to an earlier attempt.
    let (_temp, config, _, mut record) = fixture(false);
    record.set("reason", json!("stale earlier attempt"));
    record.set("exit_code", json!(9));
    fs::write(config, b"altered").unwrap();
    record.set("attempts", json!([{"status":"running"}]));
    crate::runs::finish(&mut record, Status::Completed, &json!({}), 1);
    let original = record.get("gitconfig_original_outcome").unwrap();
    assert_eq!(original["status"], "completed");
    assert!(original["reason"].is_null());
    assert!(original["exit_code"].is_null());
}

#[test]
fn missing_backlink_target_is_rejected_on_every_platform() {
    // capture() canonicalizes the backlink, so a .git that does not exist is
    // NotFound on every platform. Only `missing/../.git` differs: Windows
    // resolves `..` lexically to the real .git, Unix needs `missing` to exist.
    let (temp, _, _, record) = fixture(true);
    let cwd = temp.path().join("checkout");
    let pointer = temp.path().join("common/.git/worktrees/leaf/gitdir");
    let run = record.path().parent().unwrap();
    let missing = cwd.join("missing").join("..");
    for target in [
        temp.path().join("elsewhere/.git"),
        missing.join("other").join(".git"),
    ] {
        fs::write(&pointer, target.to_str().unwrap()).unwrap();
        let err = capture(&cwd, run).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::NotFound);
    }
    fs::write(&pointer, missing.join(".git").to_str().unwrap()).unwrap();
    assert_eq!(capture(&cwd, run).is_ok(), cfg!(windows));
    fs::write(&pointer, cwd.join(".git").to_str().unwrap()).unwrap();
    assert!(capture(&cwd, run).unwrap());
}
#[test]
fn writer_completion_protocol_table() {
    use super::*;
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let cwd = state.join("checkout");
    fs::create_dir_all(cwd.join("tools")).unwrap();
    for role in ["implement", "fix", "future_writer"] {
        for line in [
            "HOLD file_size",
            "HOLD changelog",
            "HOLD trailers",
            "HOLD scrub",
            "HOLD push_dry_run",
            "HOLD held_commit",
            "INFRA scrub",
            "HOLD preflight_unavailable",
        ] {
            fs::write(
                cwd.join("tools/preflight.py"),
                format!("print({line:?})\nraise SystemExit(1)\n"),
            )
            .unwrap();
            let mut record = RunRecord::create(
                &state,
                &pg_worker_core::run::record::new_run_id(),
                "test",
                json!({"role":role,"cwd":cwd}).as_object().unwrap().clone(),
                Status::Running,
                0.0,
            )
            .unwrap();
            assert_eq!(
                writer_completion(&mut record, Status::Completed),
                Status::Hold
            );
            assert_eq!(record.get("status"), Some(&json!("hold")));
            assert_eq!(record.get("stderr_tail"), Some(&json!(line)));
            assert_eq!(record.get("events").unwrap()[0]["event"], "writer_hold");
        }
    }
    fs::remove_dir_all(state).unwrap();
}
