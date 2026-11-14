use pg_fleet::path_check::{check, REQUIRED};
#[test]
fn missing_named_failure_repair_and_correct_path_noop() {
    let d = tempfile::tempdir().unwrap();
    let source = d.path().join("source");
    let dest = d.path().join("path");
    std::fs::create_dir(&source).unwrap();
    let dirs = vec![dest.clone()];
    assert_eq!(check(&dirs, None, None).len(), 3);
    assert!(check(&dirs, None, None)[0].starts_with("FAIL fleet PATH: handoff-check:"));
    for name in REQUIRED {
        #[cfg(windows)]
        let name = format!("{name}.exe");
        let p = source.join(name);
        std::fs::write(&p, []).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
    }
    assert!(check(&dirs, Some(&source), Some(&dest)).is_empty());
    assert!(check(&dirs, None, None).is_empty());
    assert_eq!(
        check(&[d.path().join("elsewhere")], Some(&source), Some(&dest)).len(),
        3
    );
}
