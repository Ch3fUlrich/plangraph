use super::*;

#[test]
fn legacy_target_is_removed_but_shared_cache_is_kept() {
    let root = tempfile::tempdir().unwrap();
    let tree = root.path().join("tree");
    std::fs::create_dir_all(tree.join("rust/target/debug")).unwrap();
    std::fs::write(tree.join("rust/target/debug/artifact"), b"old build").unwrap();
    std::fs::write(tree.join("rust/Cargo.toml"), b"source").unwrap();
    remove_legacy_target(&tree).unwrap();
    assert!(!tree.join("rust/target").exists());
    assert!(tree.join("rust/Cargo.toml").exists());
    remove_legacy_target(&tree).unwrap();
}

#[cfg(unix)]
#[test]
fn legacy_target_link_does_not_delete_shared_cache() {
    let root = tempfile::tempdir().unwrap();
    let tree = root.path().join("tree");
    let shared = root.path().join("shared");
    std::fs::create_dir_all(tree.join("rust")).unwrap();
    std::fs::create_dir(&shared).unwrap();
    std::fs::write(shared.join("artifact"), b"shared build").unwrap();
    std::os::unix::fs::symlink(&shared, tree.join("rust/target")).unwrap();
    remove_legacy_target(&tree).unwrap();
    assert!(shared.join("artifact").exists());
    assert!(!tree.join("rust/target").exists());
}

#[cfg(unix)]
#[test]
fn rust_directory_link_does_not_delete_external_target() {
    let root = tempfile::tempdir().unwrap();
    let tree = root.path().join("tree");
    let external = root.path().join("external");
    std::fs::create_dir(&tree).unwrap();
    std::fs::create_dir_all(external.join("target")).unwrap();
    std::fs::write(external.join("target/artifact"), b"keep").unwrap();
    std::os::unix::fs::symlink(&external, tree.join("rust")).unwrap();
    remove_legacy_target(&tree).unwrap();
    assert!(external.join("target/artifact").exists());
}
