use super::*;
use serde_json::json;

#[test]
fn reconcile_mode_precedence_and_invalid_refusal() {
    let config = json!({"reconcile":"shadow", "roles":{"review":{"reconcile":"on"}}});
    assert_eq!(mode(None, &config, "implement").unwrap(), Mode::Shadow);
    assert_eq!(mode(None, &config, "review").unwrap(), Mode::On);
    assert_eq!(mode(Some("off"), &config, "review").unwrap(), Mode::Off);
    assert!(mode(Some("invalid"), &config, "review").is_err());
    assert!(mode(None, &json!({"reconcile":true}), "review").is_err());
    assert!(mode(None, &config, "unknown").is_err());
    assert_eq!(mode(None, &json!({}), "review").unwrap(), Mode::Off);
}

#[test]
fn reconcile_off_does_not_inventory_or_create_state() {
    let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    assert!(startup(&root, Some("off"), &json!({})).is_ok());
    assert!(!root.exists());
}

#[test]
fn reconcile_on_refuses_until_authority_backend_available() {
    let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    assert!(startup(&root, Some("on"), &json!({})).is_err());
    assert!(!root.exists());
}
