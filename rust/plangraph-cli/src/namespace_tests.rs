use super::*;
#[test]
fn shim_failure_preserves_new_and_recreated_old() {
    let root = tempfile::tempdir().unwrap();
    let old = root.path().join("old");
    let new = root.path().join("new");
    fs::write(&old, b"original").unwrap();
    assert!(
        commit_move(&old, &new, true, &mut Transaction::default(), |old, _| {
            fs::write(old, b"concurrent").unwrap();
            Err(io::Error::from(io::ErrorKind::AlreadyExists))
        })
        .is_err()
    );
    assert_eq!(fs::read(old).unwrap(), b"concurrent");
    assert_eq!(fs::read(new).unwrap(), b"original");
}
#[test]
fn interrupted_single_move_reports_partial_failure() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let data = root.path().join("data");
    fs::create_dir(&home).unwrap();
    fs::write(home.join("old"), b"original").unwrap();
    let manifest = "files=[]\n[[namespace]]\nname='fixture'\nold='old'\nnew='@DATA@/new'\nkind='state'\nconsumers=[]\n";
    let (code, _) = run_with(&home, &data, manifest, Mode::Apply, |old, _| {
        fs::write(old, b"concurrent").unwrap();
        Err(io::Error::from(io::ErrorKind::AlreadyExists))
    });
    assert_eq!(code, 3);
    assert_eq!(fs::read(data.join("new")).unwrap(), b"original");
}
#[test]
fn later_shim_failure_rolls_back_every_successful_move() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let data = root.path().join("data");
    fs::create_dir(&home).unwrap();
    fs::write(home.join("first"), b"one").unwrap();
    fs::write(home.join("second"), b"two").unwrap();
    let manifest="files=[]\n[[namespace]]\nname='first'\nold='first'\nnew='@DATA@/first'\nkind='state'\nconsumers=[]\n[[namespace]]\nname='second'\nold='second'\nnew='@DATA@/second'\nkind='state'\nconsumers=[]\n";
    let (code, _) = run_with(&home, &data, manifest, Mode::Apply, |old, new| {
        if old.file_name().unwrap() == "second" {
            Err(io::Error::other("injected failure"))
        } else {
            shim(old, new)
        }
    });
    assert_eq!(code, 3);
    for name in ["first", "second"] {
        assert!(!fs::symlink_metadata(home.join(name))
            .unwrap()
            .file_type()
            .is_symlink());
        assert!(!data.join(name).exists());
    }
}
#[test]
fn metadata_errors_are_not_absence() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("loop");
    std::os::unix::fs::symlink("loop", &path).unwrap();
    assert!(present(&path.join("child")).is_err());
}
