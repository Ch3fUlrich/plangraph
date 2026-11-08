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
        fs::write(common.join("HEAD"), "ref: refs/heads/main\n").unwrap();
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
    assert_eq!(
        enforce(&mut record, Status::Completed, json!({})),
        Status::Completed
    );
    assert_eq!(record.to_value(), before);
}
#[test]
fn per_worktree_changed_restored() {
    let (_temp, _, local, mut record) = fixture(true);
    fs::write(&local, b"original local\n").unwrap();
    let snapshots_path = record.path().with_file_name("gitconfig.snapshot");
    let mut snapshots: Vec<Snapshot> =
        serde_json::from_slice(&fs::read(&snapshots_path).unwrap()).unwrap();
    snapshots[1].bytes = optional(&local).unwrap();
    atomic_write_private(&snapshots_path, &serde_json::to_vec(&snapshots).unwrap()).unwrap();
    fs::write(&local, b"changed local\n").unwrap();
    assert_eq!(
        enforce(&mut record, Status::Orphaned, json!({})),
        Status::Failed
    );
    assert_eq!(fs::read(local).unwrap(), b"original local\n");
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
    assert_eq!(
        enforce(&mut record, Status::Completed, json!({})),
        Status::Failed
    );
    assert_eq!(
        record.get("reason"),
        Some(&json!("gitconfig_snapshot_missing"))
    );
    assert_eq!(record.get("gitconfig_restore_error"), Some(&json!(true)));
}
#[test]
fn verdict_survives_retry_and_preserves_diagnostic() {
    let (temp, config, _, mut record) = fixture(false);
    fs::write(config, b"altered").unwrap();
    crate::runs::finish(
        &mut record,
        Status::Failed,
        &json!({"reason":"harness exit 7","exit_code":7}),
    );
    let mut record = RunRecord::read(temp.path(), "run-guard").unwrap();
    assert_eq!(
        enforce(&mut record, Status::Completed, json!({})),
        Status::Failed
    );
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
    assert_eq!(
        enforce(&mut record, Status::Completed, json!({})),
        Status::Failed
    );
    assert_eq!(fs::read(outside).unwrap(), b"untouched");
    assert_eq!(record.get("gitconfig_restore_error"), Some(&json!(false)));
}
#[test]
fn external_bare_worktree_and_backlink_validation() {
    let (temp, _, _, record) = fixture(true);
    let external = TempDir::new();
    let bare = external.path().join("repo.git");
    fs::rename(temp.path().join("common/.git"), &bare).unwrap();
    let cwd = temp.path().join("checkout");
    let gitdir = bare.join("worktrees/leaf");
    fs::write(cwd.join(".git"), format!("gitdir: {}", gitdir.display())).unwrap();
    assert!(capture(&cwd, record.path().parent().unwrap()).unwrap());
    fs::write(gitdir.join("gitdir"), bare.join("HEAD").to_str().unwrap()).unwrap();
    assert!(capture(&cwd, record.path().parent().unwrap()).is_err());
}
#[test]
fn retry_does_not_duplicate_alert() {
    let (_temp, config, _, mut record) = fixture(false);
    fs::write(config, b"altered").unwrap();
    enforce(&mut record, Status::Completed, json!({}));
    let before = record.to_value();
    enforce(&mut record, Status::Orphaned, json!({}));
    assert_eq!(record.to_value(), before);
}
#[cfg(unix)]
#[test]
fn persistence_failure_still_restores_and_marks_sidecar() {
    let (_temp, config, _, mut record) = fixture(false);
    let original = fs::read(&config).unwrap();
    fs::write(&config, b"altered").unwrap();
    fs::remove_file(record.path()).unwrap();
    fs::create_dir(record.path()).unwrap();
    enforce(&mut record, Status::Completed, json!({}));
    assert_eq!(fs::read(config).unwrap(), original);
    assert_eq!(record.get("gitconfig_restore_error"), Some(&json!(false)));
    assert!(record
        .path()
        .with_file_name("gitconfig.verdict-save-error")
        .exists());
}
#[test]
fn missing_snapshot_retry_is_quiet() {
    let (_temp, _, _, mut record) = fixture(false);
    fs::remove_file(sidecar(record.path().parent().unwrap())).unwrap();
    enforce(&mut record, Status::Completed, json!({}));
    let before = record.to_value();
    enforce(&mut record, Status::Orphaned, json!({}));
    assert_eq!(record.to_value(), before);
}
#[test]
fn orphan_selects_attempt_by_id_through_settle() {
    let (temp, config, _, mut record) = fixture(false);
    record.set("role", json!("implement"));
    record.set(
        "attempts",
        json!([
            {"started":1,"reason":"current","exit_code":7,
             "leg":{"leg_key":"leg","route_key":"route"}},
            {"started":0,"reason":"previous","exit_code":99}
        ]),
    );
    fs::write(config, b"altered").unwrap();
    crate::reconcile_store::settle(temp.path(), &mut record, 1, "gone", 2000).unwrap();
    let original = record.get("gitconfig_original_outcome").unwrap();
    assert_eq!(original["status"], "orphaned");
    assert_eq!(original["reason"], "current");
    assert_eq!(original["exit_code"], 7);
}
#[test]
fn resolved_windows_paths_ignore_case_but_posix_paths_do_not() {
    assert!(same_resolved_path(
        Path::new(r"H:\Repo\.git"),
        Path::new("/mnt/h/repo/.GIT")
    ));
    assert!(!same_resolved_path(
        Path::new("/repo/.git"),
        Path::new("/Repo/.git")
    ));
}
#[test]
fn missing_backlink_target_is_rejected() {
    let (temp, _, _, record) = fixture(true);
    let cwd = temp.path().join("checkout");
    fs::write(
        temp.path().join("common/.git/worktrees/leaf/gitdir"),
        cwd.join("missing/../.git").to_str().unwrap(),
    )
    .unwrap();
    assert!(capture(&cwd, record.path().parent().unwrap()).is_err());
}
#[test]
fn worktree_config_restored_or_removed() {
    for linked in [false, true] {
        let (temp, config, local, mut record) = fixture(linked);
        let path = if linked { local } else { config }.with_file_name("config.worktree");
        fs::write(&path, b"original").unwrap();
        capture(
            &temp.path().join("checkout"),
            record.path().parent().unwrap(),
        )
        .unwrap();
        fs::write(&path, b"altered").unwrap();
        enforce(&mut record, Status::Completed, json!({}));
        assert_eq!(fs::read(&path).unwrap(), b"original");
    }
    let (_temp, config, _, mut record) = fixture(false);
    let path = config.with_file_name("config.worktree");
    fs::write(&path, b"created").unwrap();
    enforce(&mut record, Status::Completed, json!({}));
    assert!(!path.exists());
}
#[cfg(unix)]
#[test]
fn worktree_config_symlink_swap_restored() {
    let (temp, config, _, mut record) = fixture(false);
    let outside = temp.path().join("outside");
    fs::write(&outside, b"untouched").unwrap();
    let path = config.with_file_name("config.worktree");
    std::os::unix::fs::symlink(&outside, &path).unwrap();
    enforce(&mut record, Status::Completed, json!({}));
    assert!(!path.exists());
    assert_eq!(fs::read(outside).unwrap(), b"untouched");
}
#[test]
fn resolve_preserves_windows_and_wsl_spellings() {
    assert_eq!(
        resolve(Path::new("base"), r"H:\repo\.git"),
        PathBuf::from("H:/repo/.git")
    );
    assert_eq!(
        resolve(Path::new("base"), "/mnt/h/repo/.git"),
        PathBuf::from("/mnt/h/repo/.git")
    );
}
#[test]
fn windows_spelled_backlink_is_accepted() {
    let (temp, _, _, record) = fixture(true);
    let cwd = temp.path().join("checkout");
    let backlink = cwd.join(".git").to_string_lossy().replace('/', "\\");
    fs::write(
        temp.path().join("common/.git/worktrees/leaf/gitdir"),
        backlink,
    )
    .unwrap();
    assert!(capture(&cwd, record.path().parent().unwrap()).unwrap());
}
#[test]
fn extended_unc_backlink_is_refused() {
    let (temp, _, _, record) = fixture(true);
    fs::write(
        temp.path().join("common/.git/worktrees/leaf/gitdir"),
        r"\\?\UNC\srv\share",
    )
    .unwrap();
    let cwd = temp.path().join("checkout");
    let error = capture(&cwd, record.path().parent().unwrap()).unwrap_err();
    assert_eq!(error.to_string(), "gitconfig_capture_refused");
}
#[test]
fn mixed_posix_windows_case_is_exact() {
    let drive = Path::new("H:/repo/.git");
    assert!(same_resolved_path(drive, Path::new("/mnt/h/repo/.git")));
    for (path, expected) in [
        ("/../../repo", "/repo"),
        ("H:/../../repo", "/mnt/h/repo"),
        ("/mnt/h/../../repo", "/mnt/h/repo"),
        ("../../repo", "../../repo"),
        ("/repo/./child/../.git", "/repo/.git"),
    ] {
        assert_eq!(normalized(Path::new(path)), expected);
    }
    assert!(!same_resolved_path(Path::new("/MNT/H/repo/.git"), drive));
}
#[test]
fn sync_file_and_directory() {
    let temp = TempDir::new();
    let file = temp.path().join("sync-file");
    fs::write(&file, b"durable").unwrap();
    sync(&file).unwrap();
    sync(temp.path()).unwrap();
}
#[cfg(unix)]
#[test]
fn layout_error_save_failure_marks_real_directory_on_every_retry() {
    let (_temp, _, _, mut record) = fixture(false);
    record.set("run_id", json!("another-run"));
    fs::remove_file(record.path()).unwrap();
    fs::create_dir(record.path()).unwrap();
    for count in 1..=2 {
        assert_eq!(
            enforce(&mut record, Status::Completed, json!({})),
            Status::Failed
        );
        assert_eq!(record.get("reason"), Some(&json!("gitconfig_guard_error")));
        let events = record.get("events").unwrap().as_array().unwrap();
        assert_eq!(events.len(), count);
        assert_eq!(events.last().unwrap()["event"], "gitconfig_guard_error");
        let marker = record.path().with_file_name("gitconfig.verdict-save-error");
        assert!(marker.is_file());
    }
}
