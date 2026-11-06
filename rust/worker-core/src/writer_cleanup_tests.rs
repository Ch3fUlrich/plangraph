use super::*;
#[test]
fn dry_run_no_prune() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = RepoEntry {
        root: tmp.path().to_string_lossy().into(),
        worktrees_root: tmp.path().to_string_lossy().into(),
        remote: "unused".into(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    assert!(janitor(&repo, tmp.path(), 90000, false).unwrap().is_empty());
    assert_eq!(fs::read_dir(tmp.path()).unwrap().count(), 0);
}
#[test]
fn seal_without_metadata_is_not_recorded() {
    let tmp = tempfile::tempdir().unwrap();
    let blob = LocalBlobStore::open(&tmp.path().join("blobs"))
        .unwrap()
        .seal(b"fixture")
        .unwrap();
    assert!(record_result(tmp.path(), "run", &blob).is_err());
    assert!(!tmp.path().join("writer-lifecycle/run.receipt").exists());
}

pub(crate) struct Fixture {
    _dir: tempfile::TempDir,
    pub(crate) repo: RepoEntry,
    pub(crate) state: PathBuf,
    pub(crate) record: LifecycleRecord,
    blob: SealedBlob,
}
impl Fixture {
    pub(crate) fn new() -> Self {
        let dir = tempfile::Builder::new()
            .prefix("cleanup-")
            .tempdir_in(std::env::current_dir().unwrap())
            .unwrap();
        let root = dir.path().join("repo");
        let trees = dir.path().join("trees");
        let checkout = trees.join("writer");
        let state = dir.path().join("state");
        for path in [&root, &trees, &state] {
            fs::create_dir(path).unwrap();
        }
        git(&root, &["init"]).unwrap();
        git(&root, &["remote", "add", "origin", "unused"]).unwrap();
        git(
            &root,
            &[
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "--allow-empty",
                "-m",
                "initial",
            ],
        )
        .unwrap();
        let sha = String::from_utf8(git(&root, &["rev-parse", "HEAD"]).unwrap()).unwrap();
        let sha = sha.trim().to_string();
        let repo = RepoEntry {
            root: root.to_string_lossy().into(),
            worktrees_root: trees.to_string_lossy().into(),
            remote: "unused".into(),
            python: None,
            import_roots: vec![],
            cache_env: Default::default(),
        };
        git(
            &root,
            &[
                "worktree",
                "add",
                "--detach",
                checkout.to_str().unwrap(),
                &sha,
            ],
        )
        .unwrap();
        let admin =
            String::from_utf8(git(&checkout, &["rev-parse", "--absolute-git-dir"]).unwrap())
                .unwrap();
        fs::write(
            Path::new(admin.trim()).join("spawner-owned"),
            serde_json::to_vec(&(
                root.canonicalize().unwrap(),
                checkout.canonicalize().unwrap(),
            ))
            .unwrap(),
        )
        .unwrap();
        let mut store = LocalBlobStore::open(&state.join("blobs")).unwrap();
        let blob = store.seal(b"fixture result").unwrap();
        store
            .bind(&crate::writer_blob::ExecutionMetadata {
                run_id: "run".into(),
                repo_id: "fixture".into(),
                checkout: checkout.clone(),
                base_sha: sha.clone(),
                head_sha: sha,
                branch: "lane/fixture".into(),
                kind: BlobKind::Result,
                blob: blob.clone(),
            })
            .unwrap();
        let record = LifecycleRecord {
            run_id: "run".into(),
            repo_id: "fixture".into(),
            checkout,
            failed_at_s: None,
            group_empty: true,
            result_blob: None,
        };
        save_lifecycle(&state, &record).unwrap();
        Self {
            _dir: dir,
            repo,
            state,
            record,
            blob,
        }
    }
}

#[test]
fn failed_24h_boundary() {
    let mut f = Fixture::new();
    f.record.failed_at_s = Some(10);
    save_lifecycle(&f.state, &f.record).unwrap();
    record_result(&f.state, "run", &f.blob).unwrap();
    assert!(!janitor(&f.repo, &f.state, 86409, true).unwrap()[0].eligible);
    assert!(f.record.checkout.exists());
    assert!(janitor(&f.repo, &f.state, 86410, true).unwrap()[0].eligible);
    assert!(!f.record.checkout.exists());
}

#[test]
fn durable_receipt_restart() {
    let f = Fixture::new();
    drop(record_result(&f.state, "run", &f.blob).unwrap());
    let cwd = std::env::current_dir().unwrap();
    let state = f.state.strip_prefix(cwd).unwrap();
    assert!(state.is_relative());
    assert!(janitor(&f.repo, state, 90000, true).unwrap()[0].eligible);
    assert!(!f.record.checkout.exists());
}

#[test]
fn unknown_or_live_group_preserved() {
    let mut f = Fixture::new();
    f.record.group_empty = false;
    f.record.failed_at_s = Some(0);
    save_lifecycle(&f.state, &f.record).unwrap();
    let receipt = record_result(&f.state, "run", &f.blob).unwrap();
    assert!(!janitor(&f.repo, &f.state, 200000, true).unwrap()[0].eligible);
    assert!(remove_recorded_writer(&f.repo, &f.record.checkout, &receipt).is_err());
    assert!(f.record.checkout.exists());
}

#[test]
fn recorded_candidate_dry_run_preserves_filesystem() {
    fn snapshot(root: &Path) -> std::collections::BTreeMap<PathBuf, Option<Vec<u8>>> {
        fn visit(
            root: &Path,
            path: &Path,
            files: &mut std::collections::BTreeMap<PathBuf, Option<Vec<u8>>>,
        ) {
            for entry in fs::read_dir(path).unwrap() {
                let entry = entry.unwrap();
                let path = entry.path();
                let directory = entry.file_type().unwrap().is_dir();
                files.insert(
                    path.strip_prefix(root).unwrap().to_path_buf(),
                    if directory {
                        None
                    } else {
                        Some(fs::read(&path).unwrap())
                    },
                );
                if directory {
                    visit(root, &path, files);
                }
            }
        }
        let mut files = std::collections::BTreeMap::new();
        visit(root, root, &mut files);
        files
    }

    let f = Fixture::new();
    record_result(&f.state, "run", &f.blob).unwrap();
    let before = snapshot(f._dir.path());
    let candidates = janitor(&f.repo, &f.state, 90000, false).unwrap();
    assert_eq!(candidates.len(), 1);
    assert!(candidates[0].eligible);
    assert_eq!(candidates[0].run_id, "run");
    assert_eq!(candidates[0].checkout, f.record.checkout);
    assert!(f.record.checkout.exists());
    assert_eq!(snapshot(f._dir.path()), before);
}

#[test]
fn daily_apply_owned_only() {
    let mut f = Fixture::new();
    f.record.failed_at_s = Some(0);
    save_lifecycle(&f.state, &f.record).unwrap();
    assert!(janitor(&f.repo, &f.state, 90000, false).unwrap()[0].eligible);
    assert!(f.record.checkout.exists());
    let stamp = common(&f.repo).unwrap().join("spawner-prune-day");
    assert!(!stamp.exists());
    janitor(&f.repo, &f.state, 90000, true).unwrap();
    assert!(!f.record.checkout.exists());
    assert!(Path::new(&f.repo.root).exists());
    assert_eq!(fs::read(&stamp).unwrap(), b"1");
    janitor(&f.repo, &f.state, 90001, true).unwrap();
    assert_eq!(fs::read(&stamp).unwrap(), b"1");
}

#[test]
fn pending_conflict_blob_never_expires() {
    for conflict in [false, true] {
        let f = Fixture::new();
        if conflict {
            record_result(&f.state, "run", &f.blob).unwrap();
            fs::write(f.state.join("writer-lifecycle/run.receipt"), b"conflict").unwrap();
        }
        assert!(!janitor(&f.repo, &f.state, i64::MAX, true).unwrap()[0].eligible);
        assert!(f.record.checkout.exists());
        assert!(LocalBlobStore::open(&f.state.join("blobs"))
            .unwrap()
            .read(&f.blob)
            .is_ok());
    }
}

#[test]
fn foreign_marker_main_symlink_refuse_each_destructive_step() {
    let f = Fixture::new();
    let receipt = record_result(&f.state, "run", &f.blob).unwrap();
    let admin =
        String::from_utf8(git(&f.record.checkout, &["rev-parse", "--absolute-git-dir"]).unwrap())
            .unwrap();
    fs::remove_file(Path::new(admin.trim()).join("spawner-owned")).unwrap();
    let sha =
        String::from_utf8(git(Path::new(&f.repo.root), &["rev-parse", "HEAD"]).unwrap()).unwrap();
    for path in [&f.record.checkout, Path::new(&f.repo.root)] {
        assert!(crate::seat_pool::reset(&f.repo, path, sha.trim()).is_err());
        assert!(remove_recorded_writer(&f.repo, path, &receipt).is_err());
        assert!(remove(&f.repo, path).is_err());
        assert!(path.exists());
    }
    #[cfg(unix)]
    {
        let link = Path::new(&f.repo.worktrees_root).join("link");
        std::os::unix::fs::symlink(&f.record.checkout, &link).unwrap();
        assert!(crate::seat_pool::reset(&f.repo, &link, sha.trim()).is_err());
        assert!(remove_recorded_writer(&f.repo, &link, &receipt).is_err());
        assert!(remove(&f.repo, &link).is_err());
    }
}

#[cfg(unix)]
#[test]
fn real_child_keeps_slot_until_group_empty() {
    use std::io::BufRead;
    use std::os::unix::process::CommandExt;
    use std::process::{Command, Stdio};
    let mut f = Fixture::new();
    let mut child = Command::new("sh")
        .args(["-c", "echo ready; read x || exit 0"])
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    // The shell remains a live group member until its input closes.
    let mut line = String::new();
    std::io::BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(line.trim(), "ready");
    let pgid = child.id() as i32;
    // Match the real Unix group probe: only ESRCH proves that the group is gone.
    let group_empty = || match crate::stall::killpg(pgid, 0) {
        Ok(()) => false,
        Err(e) if e.raw_os_error() == Some(libc::ESRCH) => true,
        Err(e) => panic!("group liveness probe failed: {e}"),
    };
    let proof = crate::seat_pool::tests::seat(&mut f);
    assert!(child.try_wait().unwrap().is_none());
    f.record.group_empty = group_empty();
    assert!(!f.record.group_empty);
    save_lifecycle(&f.state, &f.record).unwrap();
    assert!(crate::seat_pool::finish_seat(&f.repo, &proof).is_err());
    assert!(!janitor(&f.repo, &f.state, 90000, true).unwrap()[0].eligible);
    assert!(child.try_wait().unwrap().is_none());
    assert!(!group_empty());
    drop(child.stdin.take().unwrap());
    assert!(child.wait().unwrap().success());
    assert!(child.try_wait().unwrap().is_some());
    f.record = load(&f.state, "run").unwrap();
    f.record.group_empty = group_empty();
    assert!(f.record.group_empty);
    save_lifecycle(&f.state, &f.record).unwrap();
    assert!(crate::seat_pool::finish_seat(&f.repo, &proof).is_ok());
}
