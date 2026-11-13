#![cfg(unix)]
use plangraph_cli::{cost_cutover, migrate, namespace, setup::Mode};
use std::fs;
fn manifest() -> &'static str {
    "files=[]\n[[namespace]]\nname='breaker-state'\nold='legacy/breaker'\nnew='state/breaker'\nkind='state'\nconsumers=[]\n[[namespace]]\nname='remaining-state-inventory'\nold='legacy'\nnew='state'\nkind='operator'\nconsumers=[]\n"
}
#[cfg(target_os = "linux")]
#[test]
fn imports_must_precede_namespace_and_repeats_preserve_active_state() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path();
    let data = home.join("data");
    let old = home.join("legacy");
    let new = home.join("state");
    fs::create_dir_all(old.join("breaker")).unwrap();
    fs::write(old.join("breaker/watch.service.json"), r#"{"count":4}"#).unwrap();
    fs::write(old.join("daily-gate.json"), "{}").unwrap();
    let (code, rows) = namespace::run(home, &data, manifest(), Mode::Apply);
    assert_eq!(code, 2);
    assert!(rows.join(" ").contains("import"));
    assert!(!new.exists());
    migrate::breaker(&old.join("breaker"), &new.join("breaker"), false).unwrap();
    assert_eq!(namespace::run(home, &data, manifest(), Mode::Apply).0, 2);
    cost_cutover::import(&old, &new, false).unwrap();
    fs::write(new.join("breaker/watch.service.json"), r#"{"count":1}"#).unwrap();
    assert_eq!(namespace::run(home, &data, manifest(), Mode::Apply).0, 0);
    assert!(fs::symlink_metadata(old.join("breaker"))
        .unwrap()
        .file_type()
        .is_symlink());
    assert_eq!(
        fs::read_to_string(new.join("breaker/watch.service.json")).unwrap(),
        r#"{"count":1}"#
    );
    assert_eq!(
        fs::read_to_string(new.join("legacy-namespace/breaker/watch.service.json")).unwrap(),
        r#"{"count":4}"#
    );
    assert_eq!(
        migrate::breaker(&old.join("breaker"), &new.join("breaker"), false).unwrap(),
        0
    );
    assert_eq!(namespace::run(home, &data, manifest(), Mode::Apply).0, 0);
}
#[cfg(unix)]
#[test]
fn already_relocated_roots_are_idempotent_imports() {
    let root = tempfile::tempdir().unwrap();
    let old = root.path().join("old");
    let new = root.path().join("new");
    fs::create_dir_all(new.join("breaker")).unwrap();
    std::os::unix::fs::symlink(&new, &old).unwrap();
    assert_eq!(cost_cutover::import(&old, &new, false).unwrap(), 0);
    assert_eq!(
        migrate::breaker(&old.join("breaker"), &new.join("breaker"), false).unwrap(),
        0
    );
}
#[cfg(target_os = "linux")]
#[test]
fn archived_copy_rolls_back_without_touching_active_updates() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path();
    let old = home.join("legacy");
    let new = home.join("state");
    fs::create_dir_all(old.join("breaker")).unwrap();
    fs::write(old.join("breaker/u.json"), r#"{"count":4}"#).unwrap();
    migrate::breaker(&old.join("breaker"), &new.join("breaker"), false).unwrap();
    fs::write(new.join("breaker/u.json"), r#"{"count":1}"#).unwrap();
    fs::write(old.join("other"), "legacy").unwrap();
    fs::write(new.join("other"), "active").unwrap();
    let text=manifest().to_string()+"\n[[namespace]]\nname='conflict'\nold='legacy/other'\nnew='state/other'\nkind='state'\nconsumers=[]\n";
    assert_eq!(
        namespace::run(home, &home.join("data"), &text, Mode::Apply).0,
        3
    );
    assert!(!fs::symlink_metadata(old.join("breaker"))
        .unwrap()
        .file_type()
        .is_symlink());
    assert_eq!(
        fs::read_to_string(old.join("breaker/u.json")).unwrap(),
        r#"{"count":4}"#
    );
    assert_eq!(
        fs::read_to_string(new.join("breaker/u.json")).unwrap(),
        r#"{"count":1}"#
    );
    let _lock = migrate::migration_lock(&new).unwrap();
    assert!(migrate::breaker(&old.join("breaker"), &new.join("breaker"), false).is_err());
    assert_eq!(
        namespace::run(home, &home.join("data"), manifest(), Mode::Apply).0,
        2
    );
}
