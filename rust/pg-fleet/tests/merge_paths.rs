use pg_fleet::merge_paths::wrapper;
use std::path::{Path, PathBuf};
fn executable(p: &Path) {
    std::fs::write(p, "exit 0\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
}
#[test]
fn source_wrapper_resolution_cases() {
    for (kind, name, label) in [
        ("pgk", "pgk.sh", "keeper"),
        ("evidence", "ci_evidence_only.sh", "evidence"),
    ] {
        let d = tempfile::tempdir().unwrap();
        let safe = d.path().join("safe");
        std::fs::create_dir(&safe).unwrap();
        let good = safe.join(name);
        executable(&good);
        let planted = d.path().join(name);
        executable(&planted);
        let dir = d.path().join("directory");
        std::fs::create_dir(&dir).unwrap();
        for override_ in [
            None,
            Some(PathBuf::new()),
            Some(d.path().join("missing")),
            Some(dir),
        ] {
            for relative in ["", ".", "tools"] {
                let paths = vec![d.path().join("empty"), PathBuf::from(relative)];
                assert_eq!(
                    wrapper(kind, override_.as_deref(), &paths).unwrap_err(),
                    format!("{label} wrapper unavailable")
                );
                let mut paths = paths;
                paths.push(safe.clone());
                assert_eq!(
                    wrapper(kind, override_.as_deref(), &paths).unwrap(),
                    good.canonicalize().unwrap()
                );
            }
        }
        // Explicit overrides need only be files, as bash executes them.
        assert_eq!(
            wrapper(kind, Some(&planted), &[safe]).unwrap(),
            planted.canonicalize().unwrap()
        );
    }
}
#[test]
fn unavailable_wrappers_stop_cli_before_remote_calls() {
    let d = tempfile::tempdir().unwrap();
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_merge-part"))
        .args(["1", "aaaaaaaa", "p:ITEM", "D-1", "subject", "body"])
        .env("HOME", d.path())
        .env("PATH", d.path())
        .env_remove("PG_PGK")
        .env_remove("PG_CI_EVIDENCE")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(6));
    assert_eq!(out.stdout, b"STOP: keeper wrapper unavailable\n");
    assert!(out.stderr.is_empty());
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_merge_paths"))
        .arg("evidence")
        .env("PATH", d.path())
        .env_remove("PG_CI_EVIDENCE")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(6));
    assert!(out.stdout.is_empty());
    assert_eq!(out.stderr, b"STOP: evidence wrapper unavailable\n");
}
