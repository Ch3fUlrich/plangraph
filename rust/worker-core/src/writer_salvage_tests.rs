use super::*;
use crate::writer_blob::LocalBlobStore;
use crate::writer_secret::object;
fn fixture() -> (tempfile::TempDir, RepoEntry, String, String) {
    let (dir, mut repo, _, _) = crate::writer_secret::tests::fixture();
    command(
        dir.path(),
        &["checkout", "--orphan", "clean-root"],
        &[],
        None,
    )
    .unwrap();
    command(
        dir.path(),
        &["rm", "-f", "--ignore-unmatch", "é.txt"],
        &[],
        None,
    )
    .unwrap();
    std::fs::write(dir.path().join("safe"), "safe").unwrap();
    command(dir.path(), &["add", "safe"], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "clean"], &[], None).unwrap();
    let base = String::from_utf8(command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap())
        .unwrap()
        .trim()
        .to_owned();
    repo.worktrees_root = dir.path().join("trees").to_str().unwrap().into();
    std::fs::create_dir(&repo.worktrees_root).unwrap();
    repo.remote = repo.root.clone();
    command(
        dir.path(),
        &["remote", "add", "origin", &repo.remote],
        &[],
        None,
    )
    .unwrap();
    let ws = Workspace {
        repo_id: "test".into(),
        mode: Mode::Writer,
        base_sha: Some(base.clone()),
        revision_sha: Some(base.clone()),
        branch: Some("lane/test".into()),
    };
    let checkout = crate::workspace_launch::create_writer(&repo, &ws, "test").unwrap();
    repo.root = checkout.to_str().unwrap().into();
    (dir, repo, base.clone(), base)
}
fn export_salvage(
    repo: &RepoEntry,
    base: &str,
    _tip: &str,
    run: &str,
    _paths: &[u8],
) -> Result<Vec<u8>, WriterError> {
    let ws = Workspace {
        repo_id: "test".into(),
        mode: Mode::Writer,
        base_sha: Some(base.into()),
        revision_sha: Some(base.into()),
        branch: Some("lane/test".into()),
    };
    let mut registry = repo.clone();
    registry.root = repo.remote.clone();
    let store = tempfile::tempdir()?;
    let mut store = LocalBlobStore::open(&store.path().join("blobs"))?;
    let result = super::export_salvage(&registry, Path::new(&repo.root), &ws, run, &mut store)?;
    store.read(&result.blob)
}
#[test]
fn rejects_argument_injection() {
    let (_dir, repo, base, tip) = fixture();
    assert!(export_salvage(&repo, "--help", &tip, "run", b"").is_err());
    assert!(export_salvage(&repo, &base, &tip, "../run", b"").is_err());
}
#[test]
fn bundle_roundtrip_and_ref_cleanup() {
    let (_dir, repo, base, tip) = fixture();
    let dir = Path::new(&repo.root);
    let bytes = export_salvage(&repo, &base, &tip, "roundtrip", b"").unwrap();
    let bundle = dir.join("result.bundle");
    std::fs::write(&bundle, bytes).unwrap();
    command(
        dir,
        &["bundle", "verify", bundle.to_str().unwrap()],
        &[],
        None,
    )
    .unwrap();
    command(
        dir,
        &[
            "fetch",
            bundle.to_str().unwrap(),
            "refs/plangraph-salvage/roundtrip",
        ],
        &[],
        None,
    )
    .unwrap();
    let clean = command(dir, &["rev-parse", "FETCH_HEAD"], &[], None).unwrap();
    assert!(crate::writer_secret::findings(
        &repo,
        &base,
        std::str::from_utf8(&clean).unwrap().trim()
    )
    .unwrap()
    .is_empty());
    assert!(command(
        dir,
        &["show-ref", "--verify", "refs/plangraph-salvage/roundtrip"],
        &[],
        None
    )
    .is_err());
}
#[test]
fn existing_ref_is_not_overwritten() {
    let (_dir, repo, base, tip) = fixture();
    let dir = Path::new(&repo.root);
    command(
        dir,
        &["update-ref", "refs/plangraph-salvage/busy", &tip],
        &[],
        None,
    )
    .unwrap();
    assert!(export_salvage(&repo, &base, &tip, "busy", b"").is_err());
    assert_eq!(
        command(
            dir,
            &["rev-parse", "refs/plangraph-salvage/busy"],
            &[],
            None
        )
        .unwrap(),
        format!("{tip}\n").as_bytes()
    );
}
#[test]
fn real_index_and_lane_unchanged() {
    let (_dir, repo, base, tip) = fixture();
    let dir = Path::new(&repo.root);
    std::fs::write(dir.join("safe"), "staged").unwrap();
    command(dir, &["add", "safe"], &[], None).unwrap();
    std::fs::write(dir.join("safe"), "unstaged").unwrap();
    let index_path = command(dir, &["rev-parse", "--git-path", "index"], &[], None).unwrap();
    let index_path = Path::new(std::str::from_utf8(&index_path).unwrap().trim());
    let index = std::fs::read(index_path).unwrap();
    let bundle = export_salvage(&repo, &base, &tip, "run", b"safe\0").unwrap();
    assert!(bundle.starts_with(b"# v2 git bundle"));
    assert_eq!(std::fs::read(index_path).unwrap(), index);
    assert_eq!(std::fs::read(dir.join("safe")).unwrap(), b"unstaged");
    assert_eq!(
        command(dir, &["rev-parse", "HEAD"], &[], None).unwrap(),
        format!("{tip}\n").as_bytes()
    );
}

fn imported(repo: &RepoEntry, bytes: &[u8]) -> (tempfile::TempDir, RepoEntry, String) {
    let dest = tempfile::tempdir().unwrap();
    command(dest.path(), &["init", "--bare"], &[], None).unwrap();
    // Receiver has only the public prerequisite, never accesses the failed checkout.
    command(
        dest.path(),
        &["fetch", &repo.remote, "clean-root"],
        &[],
        None,
    )
    .unwrap();
    let bundle = dest.path().join("salvage.bundle");
    std::fs::write(&bundle, bytes).unwrap();
    command(
        dest.path(),
        &[
            "fetch",
            bundle.to_str().unwrap(),
            "refs/plangraph-salvage/run",
        ],
        &[],
        None,
    )
    .unwrap();
    let tip = command(dest.path(), &["rev-parse", "FETCH_HEAD"], &[], None).unwrap();
    let mut receiver = repo.clone();
    receiver.root = dest.path().to_str().unwrap().into();
    (
        dest,
        receiver,
        String::from_utf8(tip).unwrap().trim().into(),
    )
}

#[test]
fn dirty_untracked_snapshot_ignored_excluded() {
    let (_dir, repo, base, tip) = fixture();
    let root = Path::new(&repo.root);
    std::fs::write(root.join(".gitignore"), "ignored\n").unwrap();
    std::fs::write(root.join("ignored"), "not exported").unwrap();
    std::fs::write(root.join("new\nfile"), "new bytes").unwrap();
    std::fs::write(root.join("safe"), "dirty bytes").unwrap();
    let token = ["gh", "p_", &"s".repeat(25)].concat();
    std::fs::write(root.join("bad"), &token).unwrap();
    let bytes = export_salvage(&repo, &base, &tip, "run", b"").unwrap();
    let (dest, receiver, head) = imported(&repo, &bytes);
    let names = entries(dest.path(), &head).unwrap();
    assert!(!names.iter().any(|e| e.2 == b"ignored" || e.2 == b"bad"));
    for (name, expected) in [("safe", "dirty bytes"), ("new\nfile", "new bytes")] {
        let blob = &names.iter().find(|e| e.2 == name.as_bytes()).unwrap().1;
        assert_eq!(object(dest.path(), blob).unwrap().1, expected.as_bytes());
    }
    assert!(crate::writer_secret::findings(&receiver, &base, &head)
        .unwrap()
        .is_empty());
    let hash = command(root, &["hash-object", "--stdin"], token.as_bytes(), None).unwrap();
    assert!(object(root, std::str::from_utf8(&hash).unwrap().trim()).is_err());
}

#[test]
fn failed_writer_returns_blob_without_host_access() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    std::fs::write(
        root.join("committed-secret"),
        ["gh", "p_", &"t".repeat(25)].concat(),
    )
    .unwrap();
    command(root, &["add", "."], &[], None).unwrap();
    command(root, &["commit", "-m", "failed without trailer"], &[], None).unwrap();
    let old = command(root, &["rev-parse", "HEAD"], &[], None).unwrap();
    // Unchanged committed secrets must reach history sanitization, not the dirty scan.
    let bytes = export_salvage(&repo, &base, "", "run", b"").unwrap();
    let (dest, receiver, head) = imported(&repo, &bytes);
    assert!(crate::writer_secret::findings(&receiver, &base, &head)
        .unwrap()
        .is_empty());
    let objects = command(
        dest.path(),
        &["rev-list", "--objects", &head, "--not", &base],
        &[],
        None,
    )
    .unwrap();
    assert!(!String::from_utf8(objects)
        .unwrap()
        .contains(std::str::from_utf8(&old).unwrap().trim()));
    assert!(!entries(dest.path(), &head)
        .unwrap()
        .iter()
        .any(|e| e.2 == b"committed-secret"));
}

#[cfg(unix)]
#[test]
fn unsafe_symlink_and_submodule_refused() {
    let (_dir, repo, base, tip) = fixture();
    let root = Path::new(&repo.root);
    std::os::unix::fs::symlink("safe", root.join("link")).unwrap();
    assert!(export_salvage(&repo, &base, &tip, "run", b"").is_err());
    std::fs::remove_file(root.join("link")).unwrap();
    command(
        root,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{base},sub"),
        ],
        &[],
        None,
    )
    .unwrap();
    command(root, &["commit", "-m", "submodule"], &[], None).unwrap();
    std::fs::create_dir(root.join("sub")).unwrap();
    std::fs::write(root.join("sub/file"), "dirty").unwrap();
    assert!(export_salvage(&repo, &base, &tip, "run", b"").is_err());
}

#[test]
fn result_never_becomes_salvage_publication() {
    let (_dir, repo, base, tip) = fixture();
    let root = Path::new(&repo.root);
    command(
        root,
        &["commit", "--allow-empty", "-m", "no trailer"],
        &[],
        None,
    )
    .unwrap();
    let ws = Workspace {
        repo_id: "test".into(),
        mode: Mode::Writer,
        base_sha: Some(base.clone()),
        revision_sha: Some(base.clone()),
        branch: Some("lane/test".into()),
    };
    let mut registry = repo.clone();
    registry.root = repo.remote.clone();
    assert!(crate::writer_result::check_result(&registry, root, &ws).is_err());
    export_salvage(&repo, &base, &tip, "run", b"").unwrap();
    assert!(crate::writer_result::check_result(&registry, root, &ws).is_err());
}

#[test]
fn gate_before_any_snapshot_object() {
    let (_dir, repo, base, tip) = fixture();
    let root = Path::new(&repo.root);
    let safe = b"safe candidate not yet stored";
    std::fs::write(root.join("a-safe"), safe).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink("safe", root.join("z-unsafe")).unwrap();
    #[cfg(not(unix))]
    std::fs::create_dir(root.join("z-unsafe")).unwrap();
    #[cfg(unix)]
    assert!(export_salvage(&repo, &base, &tip, "run", b"").is_err());
    #[cfg(unix)]
    {
        let sha = command(root, &["hash-object", "--stdin"], safe, None).unwrap();
        assert!(object(root, std::str::from_utf8(&sha).unwrap().trim()).is_err());
    }
}
