use super::*;

fn fixture(path: &Path, old: bool) {
    fs::create_dir_all(path.join("debug/deps")).unwrap();
    fs::write(path.join(".rustc_info.json"), b"{}").unwrap();
    fs::write(path.join("debug/deps/object"), b"cache").unwrap();
    if old {
        fn age(path: &Path) {
            if path.is_dir() {
                for child in fs::read_dir(path).unwrap() {
                    age(&child.unwrap().path());
                }
            }
            fs::File::open(path)
                .unwrap()
                .set_modified(SystemTime::now() - Duration::from_secs(1801))
                .unwrap();
        }
        age(path);
    }
}
fn report() -> Report {
    Report {
        free_bytes: 0,
        alarm: false,
        dry_run: false,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    }
}

#[test]
fn idle_private_target_requires_age_lease_and_process_clearance() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("rv4-target");
    fixture(&target, false);
    let mut report = report();
    let guards = Guards::default();
    reclaim(&target, &guards, None, true, &mut report, || {
        panic!("recent target")
    })
    .unwrap();
    fixture(&target, true);
    let lease = crate::cargo_cache::try_exclusive_lease(&target)
        .unwrap()
        .unwrap();
    reclaim(&target, &guards, None, true, &mut report, || {
        panic!("leased target")
    })
    .unwrap();
    drop(lease);
    for facts in [(vec![], true), (vec![target.join("debug")], false)] {
        reclaim(&target, &guards, None, true, &mut report, || {
            Ok(facts.clone())
        })
        .unwrap();
        assert!(target.exists());
    }
    reclaim(
        &target,
        &Guards {
            pinned: vec![tmp.path().into()],
            live: vec![],
        },
        None,
        true,
        &mut report,
        || panic!("pinned target"),
    )
    .unwrap();
    reclaim(&target, &guards, None, true, &mut report, || {
        Err(io::Error::other("unavailable"))
    })
    .unwrap_err();
    assert!(target.exists());
    reclaim(&target, &guards, None, false, &mut report, || {
        Ok((vec![], false))
    })
    .unwrap();
    assert!(target.exists());
    assert_eq!(report.freed_bytes, 0);
    reclaim(&target, &guards, None, true, &mut report, || {
        Ok((vec![], false))
    })
    .unwrap();
    assert!(!target.exists());
    assert_eq!(report.freed_bytes, 7);
}

#[test]
#[cfg(unix)]
fn private_target_discovery_preserves_sources_and_symlinked_worktrees() {
    let tmp = tempfile::tempdir().unwrap();
    let temporary = tmp.path().join("tmp");
    fixture(&temporary.join(".tmp123/target"), true);
    fixture(&temporary.join("po-review-target"), true);
    let tree = tmp.path().join("tree");
    fixture(&tree.join("rust/target"), true);
    let git = |args: &[&str]| {
        assert!(std::process::Command::new("git")
            .arg("-C")
            .arg(&tree)
            .args(args)
            .status()
            .unwrap()
            .success());
    };
    git(&["init", "-q"]);
    fs::write(tree.join(".gitignore"), "target/\n").unwrap();
    let link = tmp.path().join("linked");
    fs::create_dir(&link).unwrap();
    std::os::unix::fs::symlink(tree.join("rust"), link.join("rust")).unwrap();
    let mut report = report();
    reclaim(
        &link.join("rust/target"),
        &Guards::default(),
        None,
        true,
        &mut report,
        || panic!("symlinked parent"),
    )
    .unwrap();
    assert!(tree.join("rust/target").exists());
    git(&["add", "-f", "rust/target/.rustc_info.json"]);
    sweep_using(
        Some(&temporary),
        std::slice::from_ref(&tree),
        &Guards::default(),
        true,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(tree.join("rust/target").exists());
    assert!(!temporary.join(".tmp123/target").exists());
    assert!(!temporary.join("po-review-target").exists());
    git(&["rm", "--cached", "-f", "rust/target/.rustc_info.json"]);
    fs::write(tree.join(".gitignore"), "debug/\n").unwrap();
    sweep_using(
        Some(&temporary),
        std::slice::from_ref(&tree),
        &Guards::default(),
        true,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(
        tree.join("rust/target").exists(),
        "partially ignored target contains unignored data"
    );
    fs::write(tree.join(".gitignore"), "target/\n").unwrap();
    sweep_using(
        Some(&temporary),
        std::slice::from_ref(&tree),
        &Guards::default(),
        true,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(!tree.join("rust/target").exists());
    fixture(&tree.join("rust/target"), true);
    fs::write(tree.join("rust/target/Cargo.toml"), b"source").unwrap();
    reclaim(
        &tree.join("rust/target"),
        &Guards::default(),
        None,
        true,
        &mut report,
        || panic!("source"),
    )
    .unwrap();
    assert!(tree.join("rust/target/Cargo.toml").exists());
}

#[test]
#[cfg(unix)]
fn worktree_target_older_than_last_commit_is_reclaimed_immediately() {
    let tmp = tempfile::tempdir().unwrap();
    let temporary = tmp.path().join("tmp");
    fs::create_dir_all(&temporary).unwrap();
    let tree = tmp.path().join("tree");
    fs::create_dir_all(&tree).unwrap();
    let git = |args: &[&str], commit_date: Option<&str>| {
        let mut cmd = std::process::Command::new("git");
        cmd.arg("-C")
            .arg(&tree)
            .args(["-c", "user.name=t", "-c", "user.email=t@t"])
            .args(args);
        if let Some(ts) = commit_date {
            cmd.env("GIT_COMMITTER_DATE", ts).env("GIT_AUTHOR_DATE", ts);
        }
        assert!(cmd.status().unwrap().success());
    };
    git(&["init", "-q"], None);
    fs::write(tree.join(".gitignore"), "rust/target/\n").unwrap();
    git(&["add", ".gitignore"], None);
    let now_secs = SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let commit_ts = (now_secs - 60).to_string();
    git(&["commit", "-q", "-m", "recent"], Some(&commit_ts));

    let target = tree.join("rust/target");
    fixture(&target, false);
    // Set target mtime older than the commit (now - 120s), still well under 30m.
    fn set_tree_mtime(path: &Path, when: SystemTime) {
        if path.is_dir() {
            for child in fs::read_dir(path).unwrap() {
                set_tree_mtime(&child.unwrap().path(), when);
            }
        }
        fs::File::open(path).unwrap().set_modified(when).unwrap();
    }
    set_tree_mtime(&target, SystemTime::now() - Duration::from_secs(120));

    let mut report = report();
    sweep_using(
        Some(&temporary),
        std::slice::from_ref(&tree),
        &Guards::default(),
        true,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(
        !target.exists(),
        "worktree rust/target older than HEAD commit should be swept"
    );

    // If newer than the commit and < 30m, preserve it.
    fixture(&target, false);
    sweep_using(
        Some(&temporary),
        std::slice::from_ref(&tree),
        &Guards::default(),
        true,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(target.exists(), "recent target newer than commit must stay");
}

#[test]
fn vanished_entries_are_skipped_and_other_errors_stay_errors() {
    use super::skip_vanished;
    let gone = skip_vanished(std::fs::read_dir("/nonexistent/vanished-entry")).unwrap();
    assert!(gone.is_none());
    let denied = io::Error::from(io::ErrorKind::PermissionDenied);
    assert_eq!(
        skip_vanished::<()>(Err(denied)).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    assert_eq!(skip_vanished(Ok(3)).unwrap(), Some(3));
}
