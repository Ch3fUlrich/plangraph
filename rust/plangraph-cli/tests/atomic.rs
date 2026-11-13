use plangraph_cli::atomic;
#[cfg(unix)]
use std::fs;
use std::io;
#[cfg(unix)]
#[test]
fn reused_locks_are_private_and_hard_links_are_refused() {
    use std::os::unix::fs::PermissionsExt;
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("target");
    let lock = root.path().join("target.plangraph-lock");
    fs::write(&lock, "fixture").unwrap();
    fs::set_permissions(&lock, fs::Permissions::from_mode(0o666)).unwrap();
    let guard = atomic::lock(&target).unwrap();
    assert_eq!(
        fs::metadata(&lock).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        atomic::lock(&target).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
    drop(guard);
    fs::remove_file(&lock).unwrap();
    let victim = root.path().join("victim");
    fs::write(&victim, "untouched").unwrap();
    fs::hard_link(&victim, &lock).unwrap();
    assert!(atomic::lock(&target).is_err());
    assert_eq!(fs::read(victim).unwrap(), b"untouched");
}
#[cfg(unix)]
#[test]
fn atomic_replacements_and_backups_always_have_private_modes() {
    use std::os::unix::fs::PermissionsExt;
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("target");
    fs::write(&target, "old").unwrap();
    fs::set_permissions(&target, fs::Permissions::from_mode(0o644)).unwrap();
    let _guard = atomic::lock(&target).unwrap();
    atomic::backup(&target, b"old").unwrap();
    atomic::write(&target, b"new").unwrap();
    assert_eq!(
        fs::metadata(&target).unwrap().permissions().mode() & 0o777,
        0o600
    );
    for file in fs::read_dir(root.path()).unwrap().map(Result::unwrap) {
        assert_eq!(file.metadata().unwrap().permissions().mode() & 0o777, 0o600);
    }
}

#[test]
fn persistent_lock_excludes_other_handles_and_remains_reusable() {
    let root = tempfile::tempdir().unwrap();
    let target = root.path().join("file");
    let guard = atomic::lock(&target).unwrap();
    assert_eq!(
        atomic::lock(&target).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
    drop(guard);
    assert!(root.path().join("file.plangraph-lock").is_file());
    assert!(atomic::lock(&target).is_ok());
}
