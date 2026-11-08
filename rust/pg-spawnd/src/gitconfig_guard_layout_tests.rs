use super::tests::fixture;
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
