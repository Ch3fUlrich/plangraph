use plangraph_cli::{atomic, taildrop};
use std::fs;
#[test]
fn owner_and_unsorted_reference_rules() {
    for (name, owner, rest) in [
        ("team__notes.md", "team", "notes.md"),
        ("team-2__-rf", "team-2", "_-rf"),
        ("team__..", "team", "_.."),
        ("Team__x", "unsorted", "Team__x"),
        ("..__x", "unsorted", "..__x"),
        ("a.b__x", "unsorted", "a.b__x"),
        ("a__x\ny", "unsorted", "a__x\ny"),
        ("a__", "unsorted", "a__"),
    ] {
        let (actual_owner, actual_rest, _) = taildrop::destination(name);
        assert_eq!((actual_owner.as_str(), actual_rest.as_str()), (owner, rest));
    }
}
#[test]
fn files_without_reading_payload_preserves_clashes_and_skips_symlinks() {
    let root = tempfile::tempdir().unwrap();
    let incoming = root.path().join("incoming");
    fs::create_dir(&incoming).unwrap();
    fs::write(incoming.join("team__notes"), "opaque").unwrap();
    let unknown = if cfg!(windows) {
        "unknown-name"
    } else {
        "unknown\nname"
    };
    fs::write(incoming.join(unknown), "opaque").unwrap();
    fs::create_dir(root.path().join("team")).unwrap();
    fs::write(root.path().join("team/notes"), "original").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::{symlink, PermissionsExt};
        symlink("/missing", incoming.join("link")).unwrap();
        fs::set_permissions(
            incoming.join("team__notes"),
            fs::Permissions::from_mode(0o000),
        )
        .unwrap();
    }
    assert_eq!(taildrop::run(root.path(), |_| true).unwrap(), 2);
    assert!(root.path().join("team/notes.1").exists());
    assert!(root.path().join("unsorted").join(unknown).exists());
    assert_eq!(
        fs::read_to_string(root.path().join("team/notes")).unwrap(),
        "original"
    );
    assert_eq!(
        fs::read_to_string(root.path().join("sort.log"))
            .unwrap()
            .lines()
            .count(),
        1
    );
    assert_eq!(taildrop::run(root.path(), |_| true).unwrap(), 0);
}
#[test]
fn single_fetcher_and_fetch_failure() {
    let root = tempfile::tempdir().unwrap();
    let _lock = atomic::lock(&root.path().join("fetcher")).unwrap();
    assert!(taildrop::run(root.path(), |_| panic!("must hold lock before fetching")).is_err());
    drop(_lock);
    assert_eq!(taildrop::run(root.path(), |_| false).unwrap(), 0);
}
#[cfg(unix)]
#[test]
fn refuses_destination_symlink() {
    let root = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    fs::create_dir(root.path().join("incoming")).unwrap();
    fs::write(root.path().join("incoming/team__x"), "opaque").unwrap();
    std::os::unix::fs::symlink(outside.path(), root.path().join("team")).unwrap();
    assert!(taildrop::run(root.path(), |_| true).is_err());
    assert!(root.path().join("incoming/team__x").exists());
    assert_eq!(fs::read_dir(outside.path()).unwrap().count(), 0);
}
