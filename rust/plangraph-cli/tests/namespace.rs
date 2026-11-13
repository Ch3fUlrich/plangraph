use plangraph_cli::{namespace, setup::Mode};
use std::fs;
fn manifest(kind: &str) -> String {
    format!("files=[]\n[[namespace]]\nname='fixture'\nold='legacy/config'\nnew='@DATA@/host/config'\nkind='{kind}'\nconsumers=['fixture']\n")
}
#[test]
fn check_and_dry_run_do_not_create_roots_or_read_secret_content() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let data = root.path().join("data");
    fs::create_dir_all(home.join("legacy")).unwrap();
    fs::write(home.join("legacy/config"), "opaque-secret").unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("secret"), Mode::Check).0,
        1
    );
    assert_eq!(
        namespace::run(&home, &data, &manifest("secret"), Mode::Apply).0,
        3
    );
    assert!(!data.exists());
    let (code, rows) = namespace::run(&home, &data, &manifest("non-secret"), Mode::DryRun);
    assert_eq!(code, 0);
    assert!(rows[0].contains("would-move"));
    assert!(!data.exists());
    assert!(!rows.join("").contains("opaque-secret"));
}
#[cfg(unix)]
#[test]
fn move_with_old_shim_idempotency_conflict_and_crash_recovery() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let data = root.path().join("data");
    fs::create_dir_all(home.join("legacy")).unwrap();
    fs::write(home.join("legacy/config"), "opaque").unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Apply).0,
        0
    );
    assert_eq!(
        fs::read_link(home.join("legacy/config")).unwrap(),
        data.join("host/config")
    );
    assert!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Apply).1[0].contains("skipped")
    );
    assert_eq!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Check).0,
        0
    );
    fs::remove_file(home.join("legacy/config")).unwrap(); // interrupted after rename, before shim
    assert_eq!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Apply).0,
        0
    );
    fs::remove_file(home.join("legacy/config")).unwrap();
    fs::write(home.join("legacy/config"), "old").unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Apply).0,
        2
    );
    assert_eq!(
        fs::read_to_string(data.join("host/config")).unwrap(),
        "opaque"
    );
    assert_eq!(
        fs::read_to_string(home.join("legacy/config")).unwrap(),
        "old"
    );
}
#[test]
fn inventory_has_operator_gate_and_all_consumers() {
    let manifest: plangraph_cli::setup::Manifest =
        toml::from_str(plangraph_cli::setup::MANIFEST).unwrap();
    let text = manifest
        .namespace
        .iter()
        .flat_map(|e| e.consumers.clone())
        .collect::<Vec<_>>()
        .join(" ");
    for consumer in [
        "bind mount",
        "shell profile",
        "fleet-poll",
        "ctx-watchdog",
        "hooks",
        "units",
    ] {
        assert!(text.contains(consumer));
    }
    assert_eq!(
        manifest.operator_tasks[0].task,
        "plangraph:HOST-NAMESPACE-OPERATOR"
    );
    assert_eq!(
        manifest.operator_tasks[0].requires,
        ["plangraph:HOST-NAMESPACE-MIGRATE"]
    );
    assert_eq!(manifest.operator_tasks[0].gates, ["autoos:REMOVE-AUTOOS"]);
}
#[cfg(unix)]
#[test]
fn secret_permissions_checked_metadata_only_and_symlink_ancestors_refused() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let data = root.path().join("data");
    fs::create_dir_all(home.join("legacy")).unwrap();
    fs::create_dir_all(data.join("host")).unwrap();
    fs::write(data.join("host/config"), "secret").unwrap();
    symlink(data.join("host/config"), home.join("legacy/config")).unwrap();
    fs::set_permissions(data.join("host/config"), fs::Permissions::from_mode(0o644)).unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("secret"), Mode::Check).0,
        1
    );
    fs::set_permissions(data.join("host/config"), fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("secret"), Mode::Check).0,
        0
    );
    let external = root.path().join("outside");
    fs::create_dir(&external).unwrap();
    fs::remove_file(home.join("legacy/config")).unwrap();
    fs::remove_dir(home.join("legacy")).unwrap();
    symlink(external, home.join("legacy")).unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Apply).0,
        2
    );
}
#[cfg(unix)]
#[test]
fn relative_shims_and_partial_failure_codes() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let data = root.path().join("data");
    fs::create_dir_all(home.join("legacy")).unwrap();
    fs::create_dir_all(data.join("host")).unwrap();
    fs::write(data.join("host/config"), "opaque").unwrap();
    std::os::unix::fs::symlink("../../data/host/config", home.join("legacy/config")).unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Check).0,
        0
    );
    fs::remove_file(home.join("legacy/config")).unwrap();
    fs::remove_file(data.join("host/config")).unwrap();
    fs::write(home.join("legacy/config"), "opaque").unwrap();
    fs::write(home.join("legacy/second"), "old").unwrap();
    fs::write(data.join("host/second"), "new").unwrap();
    let text = manifest("non-secret") + "\n[[namespace]]\nname='second'\nold='legacy/second'\nnew='@DATA@/host/second'\nkind='state'\nconsumers=[]\n";
    assert_eq!(namespace::run(&home, &data, &text, Mode::Apply).0, 3);
}
#[cfg(unix)]
#[test]
fn operator_root_shims_do_not_break_child_checks() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let data = root.path().join("data");
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(data.join("host")).unwrap();
    fs::write(data.join("host/config"), "opaque").unwrap();
    std::os::unix::fs::symlink(data.join("host"), home.join("legacy")).unwrap();
    assert_eq!(
        namespace::run(&home, &data, &manifest("non-secret"), Mode::Check).0,
        0
    );
}
