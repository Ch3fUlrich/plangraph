use super::*;
use serde_json::json;

struct TempDir(PathBuf);
impl TempDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn fixture(linked: bool) -> (TempDir, PathBuf, PathBuf, RunRecord) {
    let temp = TempDir::new();
    let cwd = temp.path().join("checkout");
    fs::create_dir_all(&cwd).unwrap();
    let common = temp.path().join("common/.git");
    let gitdir = common.join("worktrees/leaf");
    let config = if linked {
        fs::create_dir_all(&gitdir).unwrap();
        fs::write(cwd.join(".git"), "gitdir: ../common/.git/worktrees/leaf\n").unwrap();
        fs::write(gitdir.join("commondir"), "../..\n").unwrap();
        fs::write(gitdir.join("gitdir"), cwd.join(".git").to_str().unwrap()).unwrap();
        common.join("config")
    } else {
        fs::create_dir(cwd.join(".git")).unwrap();
        cwd.join(".git/config")
    };
    fs::write(&config, b"[core]\n\tworktree = original\n").unwrap();
    let mut record = RunRecord::create(
        temp.path(),
        "run-guard",
        "local",
        json!({}).as_object().unwrap().clone(),
        Status::Running,
        1.0,
    )
    .unwrap();
    let dir = record.path().parent().unwrap();
    assert!(capture(&cwd, dir).unwrap());
    record.set("gitconfig_guard", json!(true));
    record.save().unwrap();
    (temp, config, gitdir.join("config"), record)
}

#[test]
fn changed_common_worktree_restored_and_failed_after_restart() {
    let (temp, config, _, record) = fixture(true);
    let before = fs::read(&config).unwrap();
    fs::write(&config, b"[core]\n\tworktree = intruder\n").unwrap();
    drop(record);
    let mut record = RunRecord::read(temp.path(), "run-guard").unwrap();
    crate::runs::finish(&mut record, Status::Completed, &json!({}));
    assert_eq!(fs::read(config).unwrap(), before);
    assert_eq!(record.status(), Some(Status::Failed));
    assert_eq!(record.get("reason"), Some(&json!("gitconfig_tampered")));
    assert_eq!(
        record.get("gitconfig_original_outcome").unwrap()["status"],
        "completed"
    );
    assert!(!record.to_value().to_string().contains("intruder"));
}

#[test]
fn unchanged_outcome_untouched() {
    let (_temp, _, _, mut record) = fixture(false);
    let before = record.to_value();
    assert_eq!(enforce(&mut record, Status::Completed), Status::Completed);
    assert_eq!(record.to_value(), before);
}

#[test]
fn per_worktree_changed_restored() {
    let (_temp, _, local, mut record) = fixture(true);
    fs::write(&local, b"original local\n").unwrap();
    // Recapture after adding an existing worktree config.
    let snapshots_path = record.path().with_file_name("gitconfig.snapshot");
    let mut snapshots: Vec<Snapshot> =
        serde_json::from_slice(&fs::read(&snapshots_path).unwrap()).unwrap();
    snapshots[1].bytes = optional(&local).unwrap();
    atomic_write_private(&snapshots_path, &serde_json::to_vec(&snapshots).unwrap()).unwrap();
    fs::write(&local, b"changed local\n").unwrap();
    assert_eq!(enforce(&mut record, Status::Orphaned), Status::Failed);
    assert_eq!(fs::read(local).unwrap(), b"original local\n");
}

#[test]
fn created_during_run_removed() {
    let (_temp, _, local, mut record) = fixture(true);
    fs::write(&local, b"new local\n").unwrap();
    assert_eq!(enforce(&mut record, Status::Completed), Status::Failed);
    assert!(!local.exists());
}

#[test]
fn windows_and_wsl_pointer_paths() {
    let base = Path::new("checkout");
    for (input, expected) in [
        ("H:/repo/.git/worktrees/leaf", "H:/repo/.git/worktrees/leaf"),
        (
            r"H:\repo\.git\worktrees\leaf",
            "H:/repo/.git/worktrees/leaf",
        ),
        (
            "/mnt/h/repo/.git/worktrees/leaf",
            "/mnt/h/repo/.git/worktrees/leaf",
        ),
    ] {
        assert_eq!(resolve(base, input), PathBuf::from(expected));
    }
    assert_eq!(resolve(base, r"..\common"), base.join("../common"));
}

#[test]
fn non_checkout_noop() {
    let temp = TempDir::new();
    assert!(!capture(temp.path(), &temp.path().join("run")).unwrap());
    assert!(!temp.path().join("run").exists());
}

#[test]
fn settle_restores_and_persists_verdict() {
    let (temp, config, _, mut record) = fixture(true);
    record.set("role", json!("implement"));
    record.set(
        "attempts",
        json!([{"status":"running","started":1,
        "leg":{"leg_key":"leg","route_key":"route"}}]),
    );
    fs::write(&config, b"altered").unwrap();
    crate::reconcile_store::settle(temp.path(), &mut record, 1, "ended_unobserved", 2000).unwrap();
    let record = RunRecord::read(temp.path(), "run-guard").unwrap();
    assert_eq!(record.get("reason"), Some(&json!("gitconfig_tampered")));
    assert_eq!(record.status(), Some(Status::Failed));
    assert_eq!(
        record
            .get("events")
            .unwrap()
            .as_array()
            .unwrap()
            .last()
            .unwrap()["event"],
        "gitconfig_tampered"
    );
    assert_eq!(
        fs::read(config).unwrap(),
        b"[core]\n\tworktree = original\n"
    );
}

#[test]
fn missing_snapshot_is_explicit() {
    let (_temp, config, _, mut record) = fixture(false);
    fs::remove_file(sidecar(record.path().parent().unwrap())).unwrap();
    fs::write(&config, b"altered").unwrap();
    assert_eq!(enforce(&mut record, Status::Completed), Status::Failed);
    assert_eq!(
        record.get("reason"),
        Some(&json!("gitconfig_snapshot_missing"))
    );
    assert_eq!(record.get("gitconfig_restore_error"), Some(&json!(true)));
}

#[test]
fn verdict_survives_retry_and_preserves_diagnostic() {
    let (temp, config, _, mut record) = fixture(false);
    record.set("reason", json!("harness exit 7"));
    record.set("exit_code", json!(7));
    fs::write(config, b"altered").unwrap();
    crate::runs::finish(&mut record, Status::Failed, &json!({}));
    let mut record = RunRecord::read(temp.path(), "run-guard").unwrap();
    assert_eq!(enforce(&mut record, Status::Completed), Status::Failed);
    let original = record.get("gitconfig_original_outcome").unwrap();
    assert_eq!(original["reason"], "harness exit 7");
    assert_eq!(original["exit_code"], 7);
}

#[test]
fn submodule_without_commondir() {
    let temp = TempDir::new();
    let cwd = temp.path().join("sub");
    fs::create_dir(&cwd).unwrap();
    fs::create_dir_all(temp.path().join(".git/modules/sub")).unwrap();
    fs::write(cwd.join(".git"), "gitdir: ../.git/modules/sub").unwrap();
    assert!(capture(&cwd, &temp.path().join("run")).unwrap());
}

#[test]
fn poisoned_pointers_rejected_without_reading_configs() {
    let (temp, config, _, record) = fixture(true);
    let cwd = temp.path().join("checkout");
    let gitdir = temp.path().join("common/.git/worktrees/leaf");
    let outside = temp.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("config"), b"untouched").unwrap();
    for pointer in [
        "../../outside".to_owned(),
        outside.to_str().unwrap().to_owned(),
    ] {
        fs::write(gitdir.join("commondir"), pointer).unwrap();
        assert!(capture(&cwd, record.path().parent().unwrap()).is_err());
    }
    fs::write(cwd.join(".git"), format!("gitdir: {}", outside.display())).unwrap();
    assert!(capture(&cwd, record.path().parent().unwrap()).is_err());
    assert_eq!(fs::read(outside.join("config")).unwrap(), b"untouched");
    assert!(config.exists());
}

#[cfg(unix)]
#[test]
fn replaced_config_symlink_never_touches_target() {
    let (temp, config, _, mut record) = fixture(false);
    let outside = temp.path().join("outside");
    fs::write(&outside, b"untouched").unwrap();
    fs::remove_file(&config).unwrap();
    std::os::unix::fs::symlink(&outside, &config).unwrap();
    assert_eq!(enforce(&mut record, Status::Completed), Status::Failed);
    assert_eq!(fs::read(outside).unwrap(), b"untouched");
    assert_eq!(record.get("gitconfig_restore_error"), Some(&json!(true)));
}
