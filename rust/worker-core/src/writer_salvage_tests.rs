use super::*;
use crate::writer_blob::LocalBlobStore;
use crate::writer_sanitize::sanitize_history;
pub(super) fn fixture() -> (tempfile::TempDir, RepoEntry, String, String) {
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
pub(super) fn export_salvage(
    repo: &RepoEntry,
    base: &str,
    run: &str,
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
fn distinct_secret_names_survive_export_merge() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    let names: Vec<_> = ['a', 'b', 'c']
        .into_iter()
        .map(|c| ["gh", "p_", &c.to_string().repeat(25)].concat())
        .collect();
    for name in &names[..2] {
        std::fs::write(root.join(name), "safe bytes").unwrap();
    }
    command(root, &["add", "."], &[], None).unwrap();
    command(root, &["commit", "-m", "secret names"], &[], None).unwrap();
    for name in &names {
        std::fs::write(root.join(name), "dirty bytes").unwrap();
    }
    let tip = String::from_utf8(command(root, &["rev-parse", "HEAD"], &[], None).unwrap()).unwrap();
    let (_, raw_dropped) = snapshot(root, &base, tip.trim()).unwrap();
    assert_eq!(
        raw_dropped,
        names
            .iter()
            .map(|name| name.as_bytes().to_vec())
            .collect::<Vec<_>>()
    );
    let ws = Workspace {
        repo_id: "test".into(),
        mode: Mode::Writer,
        base_sha: Some(base.clone()),
        revision_sha: Some(base),
        branch: Some("lane/test".into()),
    };
    let mut registry = repo.clone();
    registry.root = repo.remote.clone();
    let store = tempfile::tempdir().unwrap();
    let mut store = LocalBlobStore::open(&store.path().join("blobs")).unwrap();
    let result = super::export_salvage(&registry, root, &ws, "names", &mut store).unwrap();
    assert_eq!(
        result.dropped_paths,
        vec!["<path removed by secret gate>"; 3]
    );
}
#[test]
fn rejects_argument_injection() {
    let (_dir, repo, base, _) = fixture();
    assert!(export_salvage(&repo, "--help", "run").is_err());
    assert!(export_salvage(&repo, &base, "../run").is_err());
}
#[test]
fn bundle_failure_runs_cleanup_and_reports_cleanup_failure() {
    let (_dir, repo, _, tip) = fixture();
    let root = Path::new(&repo.root);
    let reference = "refs/plangraph-salvage/failure";
    for locked in [false, true] {
        command(root, &["update-ref", reference, &tip], &[], None).unwrap();
        let path = command(root, &["rev-parse", "--git-path", reference], &[], None).unwrap();
        let lock = format!("{}.lock", std::str::from_utf8(&path).unwrap().trim());
        if locked {
            std::fs::write(&lock, "locked").unwrap();
        }
        let result = command(
            root,
            &["bundle", "create", "/dev/null/missing", reference],
            &[],
            None,
        );
        assert!(result.is_err());
        let outcome = finish_bundle(result, || {
            command(root, &["update-ref", "-d", reference, &tip], &[], None)
        });
        if locked {
            assert!(matches!(
                outcome,
                Err(WriterError::SalvageCleanup {
                    bundle: Some(bundle), cleanup
                }) if matches!(*bundle, WriterError::Git) && matches!(*cleanup, WriterError::Git)
            ));
            std::fs::remove_file(&lock).unwrap();
            command(root, &["update-ref", "-d", reference, &tip], &[], None).unwrap();
        } else {
            assert!(matches!(outcome, Err(WriterError::Git)));
        }
        assert!(command(root, &["show-ref", "--verify", reference], &[], None).is_err());
    }
}
#[test]
fn finish_bundle_preserves_both_error_kinds() {
    for failed in [false, true] {
        let bundle = if failed {
            Err(WriterError::Invalid)
        } else {
            Ok(vec![])
        };
        let outcome = finish_bundle(bundle, || Err(WriterError::Git));
        match outcome {
            Err(WriterError::SalvageCleanup { bundle, cleanup }) => {
                assert!(matches!(*cleanup, WriterError::Git));
                if failed {
                    assert!(matches!(bundle.as_deref(), Some(WriterError::Invalid)));
                } else {
                    assert!(bundle.is_none());
                }
            }
            other => panic!("expected cleanup failure, got {other:?}"),
        }
    }
    assert!(matches!(
        finish_bundle(Err(WriterError::Invalid), || Ok(vec![])),
        Err(WriterError::Invalid)
    ));
    assert!(finish_bundle(Ok(vec![]), || Ok(vec![])).is_ok());
}
#[test]
fn bundle_roundtrip_and_ref_cleanup() {
    let (_dir, repo, base, _) = fixture();
    let dir = Path::new(&repo.root);
    let bytes = export_salvage(&repo, &base, "roundtrip").unwrap();
    let bundle = dir.join("result.bundle");
    std::fs::write(&bundle, bytes).unwrap();
    // The owned checkout is canonical (verbatim on Windows); Git needs native paths.
    let bundle = crate::workspace_launch::git_worktree_path(&bundle);
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
    assert!(export_salvage(&repo, &base, "busy").is_err());
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
    let bundle = export_salvage(&repo, &base, "run").unwrap();
    assert!(bundle.starts_with(b"# v2 git bundle"));
    assert_eq!(std::fs::read(index_path).unwrap(), index);
    assert_eq!(std::fs::read(dir.join("safe")).unwrap(), b"unstaged");
    assert_eq!(
        command(dir, &["rev-parse", "HEAD"], &[], None).unwrap(),
        format!("{tip}\n").as_bytes()
    );
}

pub(super) fn imported(repo: &RepoEntry, bytes: &[u8]) -> (tempfile::TempDir, RepoEntry, String) {
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
fn validate_paths_refuses_unsafe_names_and_missing_nul() {
    for paths in [
        &b"safe\\name\0"[..],
        &b"-option\0"[..],
        &b"./safe\0"[..],
        &b"nested/./safe\0"[..],
        &b"nested/../safe\0"[..],
        &b"safe"[..],
    ] {
        assert!(matches!(validate_paths(paths), Err(WriterError::Invalid)));
    }
    assert!(validate_paths(b"safe\0nested/file\0").is_ok());
    assert!(validate_paths(b"").is_ok());
}

#[test]
fn unrelated_head_refused_before_snapshot() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    command(root, &["checkout", "--orphan", "foreign"], &[], None).unwrap();
    command(root, &["commit", "-m", "foreign"], &[], None).unwrap();
    let tip = command(root, &["rev-parse", "HEAD"], &[], None).unwrap();
    let tip = std::str::from_utf8(&tip).unwrap().trim();
    assert!(matches!(
        sanitize_history(&repo, &base, tip),
        Err(WriterError::Verify)
    ));
    // An unsafe dirty path would fail snapshot with Invalid if it were reached.
    std::fs::write(root.join("-unsafe"), "safe").unwrap();
    assert!(matches!(
        export_salvage(&repo, &base, "run"),
        Err(WriterError::Verify)
    ));
}
