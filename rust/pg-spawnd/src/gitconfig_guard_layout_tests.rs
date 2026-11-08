use super::tests::{fixture, TempDir};
use super::*;
use serde_json::json;

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
    crate::runs::finish(&mut record, Status::Completed, &json!({}));
    let original = record.get("gitconfig_original_outcome").unwrap();
    assert_eq!(original["status"], "completed");
    assert!(original["reason"].is_null());
    assert!(original["exit_code"].is_null());
}

#[test]
fn missing_backlink_target_is_rejected_on_every_platform() {
    // Windows counterpart of the cfg(unix) `missing/..` case: a backlink
    // naming a .git that does not exist is refused, not resolved lexically.
    let (temp, _, _, record) = fixture(true);
    let cwd = temp.path().join("checkout");
    fs::write(
        temp.path().join("common/.git/worktrees/leaf/gitdir"),
        temp.path().join("elsewhere/.git").to_str().unwrap(),
    )
    .unwrap();
    assert!(capture(&cwd, record.path().parent().unwrap()).is_err());
    let lexical = cwd.join("missing").join("..").join("other").join(".git");
    fs::write(
        temp.path().join("common/.git/worktrees/leaf/gitdir"),
        lexical.to_str().unwrap(),
    )
    .unwrap();
    assert!(capture(&cwd, record.path().parent().unwrap()).is_err());
}
