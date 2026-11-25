//! The credential probe command against the real Codex sandbox, with fixture files only.
// Every test of this file runs a shell fixture or the Linux sandbox: the imports follow them.
#[cfg(unix)]
use pg_worker_core::credential_isolation::{deny_read_paths, plangraph_config_dirs, probe};
#[cfg(unix)]
use std::{fs, path::Path};

/// The fixture config directory opens as `denied` from inside the sandbox, the control as
/// `readable`, under the effective writer policy.
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
    let required = plangraph_config_dirs(&home, None);
    let (lines, ok) = probe(Path::new("codex"), exe, scratch.path(), &deny, &required).unwrap();
    assert!(ok, "{lines:?}");
    assert_eq!(lines[0], "readable control");
    assert!(
        lines.contains(&format!("denied {}", config.display())),
        "{lines:?}"
    );
}

/// A fixture `codex` that runs the command after `--` without any sandbox: the control opens
/// as `readable`, every probed path does too.
#[cfg(unix)]
fn passthrough_codex(dir: &Path) -> std::path::PathBuf {
    let codex = dir.join("codex");
    pg_worker_core::executable_test_support::write_executable(
        &codex,
        "#!/bin/sh\nwhile [ \"$1\" != -- ]; do shift; done\nshift\nexec \"$@\"\n",
    );
    codex
}

/// No existing plangraph config directory: the probe never claims isolation, even though the
/// control reads and another listed path may be denied.
#[cfg(unix)]
#[test]
fn a_host_with_no_config_directory_is_not_reported_as_isolated() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("empty-home");
    let other = home.join(".ssh");
    fs::create_dir_all(&other).unwrap();
    let exe = Path::new(env!("CARGO_BIN_EXE_pg-host-readiness"));
    let (lines, ok) = probe(
        &passthrough_codex(dir.path()),
        exe,
        dir.path(),
        &deny_read_paths(&home, None),
        &plangraph_config_dirs(&home, None),
    )
    .unwrap();
    assert!(!ok, "{lines:?}");
    assert_eq!(lines[0], "readable control");
    assert!(
        lines
            .iter()
            .any(|l| l.starts_with("absent no plangraph config")),
        "{lines:?}"
    );
}

/// A sandbox that does not deny: the config directory opens as `readable`, not isolated.
#[cfg(unix)]
#[test]
fn a_sandbox_that_does_not_deny_is_not_reported_as_isolated() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("fixture-home");
    fs::create_dir_all(home.join(".config/plangraph")).unwrap();
    let exe = Path::new(env!("CARGO_BIN_EXE_pg-host-readiness"));
    let (lines, ok) = probe(
        &passthrough_codex(dir.path()),
        exe,
        dir.path(),
        &deny_read_paths(&home, None),
        &plangraph_config_dirs(&home, None),
    )
    .unwrap();
    assert!(!ok, "{lines:?}");
    assert!(
        lines.contains(&format!(
            "readable {}",
            home.join(".config/plangraph").display()
        )),
        "{lines:?}"
    );
}
