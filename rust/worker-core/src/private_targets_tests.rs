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
    reclaim(
        &target,
        &guards,
        SystemTime::now(),
        true,
        &mut report,
        || panic!("recent target"),
    )
    .unwrap();
    fixture(&target, true);
    let lease = crate::cargo_cache::try_exclusive_lease(&target)
        .unwrap()
        .unwrap();
    reclaim(
        &target,
        &guards,
        SystemTime::now(),
        true,
        &mut report,
        || panic!("leased target"),
    )
    .unwrap();
    drop(lease);
    for facts in [(vec![], true), (vec![target.join("debug")], false)] {
        reclaim(
            &target,
            &guards,
            SystemTime::now(),
            true,
            &mut report,
            || Ok(facts.clone()),
        )
        .unwrap();
        assert!(target.exists());
    }
    reclaim(
        &target,
        &Guards {
            pinned: vec![tmp.path().into()],
            live: vec![],
        },
        SystemTime::now(),
        true,
        &mut report,
        || panic!("pinned target"),
    )
    .unwrap();
    reclaim(
        &target,
        &guards,
        SystemTime::now(),
        true,
        &mut report,
        || Err(io::Error::other("unavailable")),
    )
    .unwrap_err();
    assert!(target.exists());
    reclaim(
        &target,
        &guards,
        SystemTime::now(),
        false,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(target.exists());
    assert_eq!(report.freed_bytes, 0);
    reclaim(
        &target,
        &guards,
        SystemTime::now(),
        true,
        &mut report,
        || Ok((vec![], false)),
    )
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
        SystemTime::now(),
        true,
        &mut report,
        || panic!("symlinked parent"),
    )
    .unwrap();
    assert!(tree.join("rust/target").exists());
    git(&["add", "-f", "rust/target/.rustc_info.json"]);
    sweep_using(
        &temporary,
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
        &temporary,
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
        &temporary,
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
        SystemTime::now(),
        true,
        &mut report,
        || panic!("source"),
    )
    .unwrap();
    assert!(tree.join("rust/target/Cargo.toml").exists());
}
