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
    let common = temp.path().join("common");
    let gitdir = common.join("worktrees/leaf");
    let config = if linked {
        fs::create_dir_all(&gitdir).unwrap();
        fs::write(cwd.join(".git"), "gitdir: ../common/worktrees/leaf\n").unwrap();
        fs::write(gitdir.join("commondir"), "../..\n").unwrap();
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
