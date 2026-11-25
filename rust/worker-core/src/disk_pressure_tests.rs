use super::*;

fn target(tree: &Path, bytes: usize, old: bool) -> Candidate {
    let target = tree.join("rust/target");
    fs::create_dir_all(target.join("debug/deps")).unwrap();
    fs::write(target.join(".rustc_info.json"), b"{}").unwrap();
    fs::write(target.join("debug/deps/object"), vec![0u8; bytes]).unwrap();
    if old {
        let past = SystemTime::now() - Duration::from_secs(3600);
        for path in [
            target.join("debug/deps/object"),
            target.join("debug/deps"),
            target.join("debug"),
            target.join(".rustc_info.json"),
            target.clone(),
        ] {
            fs::File::open(path).unwrap().set_modified(past).unwrap();
        }
    }
    Candidate {
        target,
        owner: tree.to_path_buf(),
        keep_root: false,
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
fn a_build_in_tree_a_keeps_a_and_the_idle_target_of_tree_b_goes() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let (a, b) = (root.join("tree-a"), root.join("tree-b"));
    let ca = target(&a, 4000, true);
    let cb = target(&b, 1000, true);
    // The build process works in tree A, and the host-wide "a build is running" fact is true.
    let refs = vec![a.join("rust")];
    let mut report = report();
    let kept = reclaim(
        &[ca, cb],
        &Guards::default(),
        &refs,
        (10, 500),
        &|| None,
        SystemTime::now(),
        true,
        &mut report,
    );
    assert!(!b.join("rust/target").exists(), "{:?}", report.actions);
    assert!(a.join("rust/target").exists());
    assert_eq!(report.freed_bytes, 1000 + 2);
    let row = kept.iter().find(|k| k.path.starts_with(&a)).unwrap();
    assert!(row.reason.starts_with("busy"), "{}", row.reason);
}

#[test]
fn removal_stops_once_free_space_is_back_above_the_floor_largest_first() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let trees: Vec<_> = ["big", "mid", "small"].map(|n| root.join(n)).into();
    let sizes = [9000, 5000, 100];
    let candidates: Vec<_> = trees
        .iter()
        .zip(sizes)
        .map(|(t, n)| target(t, n, true))
        .collect();
    let mut report = report();
    let kept = reclaim(
        &candidates,
        &Guards::default(),
        &[],
        (0, 8000),
        &|| None,
        SystemTime::now(),
        true,
        &mut report,
    );
    assert!(!trees[0].join("rust/target").exists());
    assert!(trees[1].join("rust/target").exists() && trees[2].join("rust/target").exists());
    assert!(report.freed_bytes >= 9000);
    assert!(kept
        .iter()
        .all(|k| k.reason == "free space is above the floor"));
}

#[test]
fn nothing_removable_names_every_kept_path_with_its_reason() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let (pinned, fresh, busy) = (root.join("pinned"), root.join("fresh"), root.join("busy"));
    let candidates = [
        target(&pinned, 300, true),
        target(&fresh, 200, false),
        target(&busy, 100, true),
    ];
    let guards = Guards {
        pinned: vec![pinned.clone()],
        live: vec![],
    };
    let mut report = report();
    let kept = reclaim(
        &candidates,
        &guards,
        std::slice::from_ref(&busy),
        (0, 10_000),
        &|| None,
        SystemTime::now(),
        true,
        &mut report,
    );
    assert_eq!(report.freed_bytes, 0);
    let reasons: Vec<_> = kept.iter().map(|k| k.reason.as_str()).collect();
    assert!(reasons[0].starts_with("pinned"), "{reasons:?}");
    assert!(reasons[1].starts_with("touched"), "{reasons:?}");
    assert!(reasons[2].starts_with("busy"), "{reasons:?}");
    let alert = stalled_alert(&kept);
    assert!(alert.contains("freed 0") && alert.contains("pinned") && alert.contains("busy"));
    assert_eq!(report.skipped.len(), 3, "one reason line per kept path");
    assert!(stalled_alert(&[]).contains("no Cargo target candidate"));
}

#[test]
fn a_symlinked_parent_never_redirects_removal_outside_the_worktree() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let outside = root.join("outside");
    let external = target(&outside, 700, true);
    let tree = root.join("tree");
    fs::create_dir_all(&tree).unwrap();
    // tree/rust -> outside/rust, so tree/rust/target is the external target.
    std::os::unix::fs::symlink(outside.join("rust"), tree.join("rust")).unwrap();
    let candidate = Candidate {
        target: tree.join("rust/target"),
        owner: tree.clone(),
        keep_root: false,
    };
    let mut report = report();
    let kept = reclaim(
        &[candidate],
        &Guards::default(),
        &[],
        (0, 10_000),
        &|| None,
        SystemTime::now(),
        true,
        &mut report,
    );
    assert!(external.target.join("debug/deps/object").exists());
    assert_eq!(report.freed_bytes, 0);
    assert!(kept[0].reason.contains("outside"), "{}", kept[0].reason);
}

#[test]
fn a_mount_point_target_keeps_its_root_and_counts_what_was_cleared() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let mut candidate = target(&root.join("tree"), 700, true);
    candidate.keep_root = true; // stands for a target that is a mount point
    let mut report = report();
    let _ = reclaim(
        &[candidate],
        &Guards::default(),
        &[],
        (0, 10_000),
        &|| None,
        SystemTime::now(),
        true,
        &mut report,
    );
    let dir = root.join("tree/rust/target");
    assert!(dir.is_dir(), "the mount root survives");
    assert_eq!(
        fs::read_dir(&dir).unwrap().count(),
        0,
        "its contents are gone"
    );
    assert!(report.freed_bytes >= 700, "{}", report.freed_bytes);
}
