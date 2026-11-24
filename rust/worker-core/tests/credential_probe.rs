//! The credential probe command against the real Codex sandbox, with fixture files only.
use pg_worker_core::credential_isolation::{deny_read_paths, probe};
use std::{fs, path::Path};

/// The fixture config directory opens as `denied` from inside the sandbox, the control as
/// `readable`, and the binary's own `--credential-probe` is built on the same function.
#[cfg(target_os = "linux")]
#[test]
#[ignore = "live host check requiring a working Codex sandbox"]
fn the_probe_reports_denied_for_a_fixture_config_directory() {
    let dir = tempfile::tempdir_in(env!("CARGO_MANIFEST_DIR")).unwrap();
    let home = fs::canonicalize(dir.path()).unwrap();
    let config = home.join(".config/plangraph");
    fs::create_dir_all(&config).unwrap();
    fs::write(config.join("keeper.dsn"), "fixture-secret").unwrap();
    let exe = Path::new(env!("CARGO_BIN_EXE_pg-host-readiness"));
    // The sandbox's writable project root must not contain a denied path.
    let scratch = tempfile::tempdir().unwrap();
    let deny = deny_read_paths(&home, None);
    let (lines, ok) = probe(Path::new("codex"), exe, scratch.path(), &deny).unwrap();
    assert!(ok, "{lines:?}");
    assert_eq!(lines[0], "readable control");
    assert!(
        lines.contains(&format!("denied {}", config.display())),
        "{lines:?}"
    );
}

/// The probe of a missing secret set never claims isolation.
#[test]
fn a_host_with_no_secret_path_is_not_reported_as_isolated() {
    let dir = tempfile::tempdir().unwrap();
    let exe = Path::new(env!("CARGO_BIN_EXE_pg-host-readiness"));
    let (lines, ok) = probe(
        Path::new("/nonexistent-codex"),
        exe,
        dir.path(),
        &deny_read_paths(&dir.path().join("empty-home"), None),
    )
    .unwrap_or_else(|_| (vec!["unavailable".into()], false));
    assert!(!ok, "{lines:?}");
}
