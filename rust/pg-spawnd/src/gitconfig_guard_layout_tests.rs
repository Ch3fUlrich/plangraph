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
