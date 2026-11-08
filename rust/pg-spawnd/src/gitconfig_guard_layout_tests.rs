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
    let (_temp, _, local, mut record) = fixture(true);
    fs::write(&local, b"new local\n").unwrap();
    assert_eq!(
        enforce(&mut record, Status::Completed, json!({})),
        Status::Failed
    );
    assert!(!local.exists());
}

#[test]
fn non_checkout_noop() {
    let temp = TempDir::new();
    assert!(!capture(temp.path(), &temp.path().join("run")).unwrap());
    assert!(!temp.path().join("run").exists());
}

#[test]
fn record_api_always_supplies_a_parent() {
    let (_temp, _, _, record) = fixture(false);
    // RunRecord's private path is constructed as run_dir(...).join("record.json").
    assert_eq!(record.path().file_name().unwrap(), "record.json");
    assert!(record.path().parent().is_some());
    assert!(run_dir(Path::new(""), "run-guard")
        .unwrap()
        .join("record.json")
        .parent()
        .is_some());
}
