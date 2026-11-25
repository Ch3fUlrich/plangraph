//! Tests of the pg-disk-hygiene binary (a child module, so `super` is the binary).
use super::*;
use std::{fs, time::Duration};

#[test]
fn cli_arguments_sweep_two_roots_even_with_an_offline_mount() {
    let fixture = tempfile::tempdir().unwrap();
    let path = |name| fixture.path().join(name);
    let repo = path("repo");
    fs::create_dir(&repo).unwrap();
    let git = |args: &[&str]| {
        pg_worker_core::host_command::output(
            pg_worker_core::git_test_support::command(&repo)
                .args([
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=test@example.invalid",
                ])
                .args(args),
            Duration::from_secs(10),
        )
        .unwrap();
    };
    git(&["init", "-q"]);
    fs::write(repo.join(".gitignore"), "target/\n").unwrap();
    git(&["add", ".gitignore"]);
    git(&["commit", "-qm", "fixture"]);
    let mut expected = Vec::new();
    let roots = [path("first"), path("second")];
    let head = disk_hygiene::git(&repo, &["rev-parse", "HEAD"]).unwrap();
    let stamp = pg_worker_core::worktree::fast_check_stamp(&repo, head.trim()).unwrap();
    let stranded = stamp.with_file_name("check-stranded-fast");
    let siblings = [roots[0].join("lane"), roots[1].join("lane")];
    for tree in siblings.iter().chain([&stranded]) {
        fs::create_dir_all(tree.parent().unwrap()).unwrap();
        git(&["worktree", "add", "--detach", tree.to_str().unwrap()]);
        let target = tree.join("target");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("cache"), "cached").unwrap();
        let past = SystemTime::now() - Duration::from_secs(25 * 3600);
        for file in [
            tree,
            &tree.join(".git"),
            &tree.join(".gitignore"),
            &target,
            &target.join("cache"),
        ] {
            fs::File::open(file).unwrap().set_modified(past).unwrap();
        }
        if tree != &stranded {
            expected.push(target);
        }
    }
    let pins = path("pins.json");
    fs::write(&pins, "[]").unwrap();
    let offline = path("offline");
    let args = [
        "--repo",
        repo.to_str().unwrap(),
        "--pins",
        pins.to_str().unwrap(),
        "--root",
        roots[0].to_str().unwrap(),
        "--root",
        roots[1].to_str().unwrap(),
        "--alarm-free-bytes",
        "1",
        "--cleanup-free-bytes",
        "18446744073709551615",
        "--dry-run",
    ]
    .map(str::to_owned);
    let report = execute_with(
        &args,
        fixture.path(),
        vec![offline.clone()],
        || Ok((vec![], false)),
        || Ok((vec![], vec![])),
    )
    .unwrap();
    assert!(report.alarm);
    assert!(report
        .skipped
        .iter()
        .any(|message| message.contains(offline.to_str().unwrap())));
    for target in expected {
        assert!(report
            .actions
            .iter()
            .any(|message| message.contains(target.to_str().unwrap())));
        assert!(target.is_dir());
    }
    fs::write(&stamp, &head).unwrap();
    let mut args = args;
    *args.last_mut().unwrap() = "--apply".into();
    let report = execute_with(
        &args,
        fixture.path(),
        vec![],
        || Ok((vec![], false)),
        || Ok((vec![], vec![])),
    )
    .unwrap();
    assert!(!stranded.exists(), "{report:?}");
    assert_eq!(fs::read_to_string(&stamp).unwrap(), head);
    let listing = disk_hygiene::git(&repo, &["worktree", "list", "--porcelain"]).unwrap();
    assert!(!listing.contains("check-stranded-fast"), "{listing}");
}

/// Two idle worktrees with a Cargo target each; `busy` names the trees a process works in.
fn pressure_fixture() -> (tempfile::TempDir, PathBuf, PathBuf, Vec<String>) {
    let fixture = tempfile::tempdir().unwrap();
    let repo = fixture.path().join("repo");
    let root = fixture.path().join("trees");
    fs::create_dir_all(&repo).unwrap();
    fs::create_dir_all(&root).unwrap();
    let git = |args: &[&str]| {
        pg_worker_core::host_command::output(
            pg_worker_core::git_test_support::command(&repo)
                .args([
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=test@example.invalid",
                ])
                .args(args),
            Duration::from_secs(10),
        )
        .unwrap();
    };
    git(&["init", "-qb", "main"]);
    fs::write(repo.join(".gitignore"), "target/\n").unwrap();
    git(&["add", ".gitignore"]);
    git(&["commit", "-qm", "fixture"]);
    git(&["update-ref", "refs/remotes/origin/main", "HEAD"]);
    let past = SystemTime::now() - Duration::from_secs(3600);
    for name in ["tree-a", "tree-b"] {
        let tree = root.join(name);
        git(&["worktree", "add", "-qb", name, tree.to_str().unwrap()]);
        let target = tree.join("rust/target");
        fs::create_dir_all(target.join("debug")).unwrap();
        fs::write(target.join(".rustc_info.json"), "{}").unwrap();
        fs::write(target.join("debug/object"), vec![0u8; 4096]).unwrap();
        for file in [
            target.join("debug/object"),
            target.join("debug"),
            target.join(".rustc_info.json"),
            target.clone(),
        ] {
            fs::File::open(file).unwrap().set_modified(past).unwrap();
        }
    }
    let pins = fixture.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    let args = [
        "--repo",
        repo.to_str().unwrap(),
        "--pins",
        pins.to_str().unwrap(),
        "--root",
        root.to_str().unwrap(),
        "--alarm-free-bytes",
        "1",
        "--cleanup-free-bytes",
        "18446744073709551615",
        "--apply",
    ]
    .map(str::to_owned)
    .to_vec();
    (fixture, repo, root, args)
}

#[test]
fn a_build_in_tree_a_does_not_defer_the_cleanup_of_idle_tree_b() {
    let (fixture, _repo, root, args) = pressure_fixture();
    let tree_a = root.canonicalize().unwrap().join("tree-a");
    let report = execute_with(
        &args,
        fixture.path(),
        vec![],
        // The host-wide "building" fact is true; only tree A has a process.
        || Ok((vec![tree_a.join("rust")], true)),
        || Ok((vec![], vec![])),
    )
    .unwrap();
    assert!(!root.join("tree-b/rust/target").exists(), "{report:?}");
    assert!(root.join("tree-a/rust/target").exists());
    assert!(
        report.freed_bytes > 0 && report.error.is_none(),
        "{report:?}"
    );
    assert!(
        report
            .skipped
            .iter()
            .any(|m| m.contains("tree-a") && m.contains("kept: busy")),
        "{:?}",
        report.skipped
    );
}

#[test]
fn below_the_floor_with_freed_zero_the_run_fails_naming_kept_paths_and_reasons() {
    let (fixture, _repo, root, args) = pressure_fixture();
    let canonical = root.canonicalize().unwrap();
    let refs = vec![canonical.join("tree-a/rust"), canonical.join("tree-b")];
    let report = execute_with(
        &args,
        fixture.path(),
        vec![],
        || Ok((refs.clone(), true)),
        || Ok((vec![], vec![])),
    )
    .unwrap();
    assert_eq!(report.freed_bytes, 0);
    let error = report.error.expect("freed 0 below the floor is an error");
    assert!(
        error.contains("freed 0") && error.contains("tree-a") && error.contains("busy"),
        "{error}"
    );
    assert!(report.alarm);
}

/// Protection that reads the same on every reload: nothing pinned, running or mounted.
fn calm() -> io::Result<disk_pressure::Protection> {
    Ok(Default::default())
}

fn floor_3000() -> Thresholds {
    Thresholds {
        alarm_free_bytes: 1,
        cleanup_free_bytes: 3000,
    }
}

#[test]
fn a_measurement_that_disagrees_with_the_estimate_wins_both_ways() {
    let (fixture, repo, root, _args) = pressure_fixture();
    let canonical = root.canonicalize().unwrap();
    let targets = ["tree-a", "tree-b"].map(|n| canonical.join(n).join("rust/target"));
    let roots = [canonical.clone(), fixture.path().canonicalize().unwrap()];
    let removed = |targets: &[PathBuf; 2]| targets.iter().filter(|t| !t.exists()).count();

    // The estimate (4098 bytes gained per removal) would stop after one removal against a
    // floor of 3000, but the filesystem still reports 100 bytes free: both targets go. The
    // probe is a directory that survives the removals, never the removed target itself.
    let low = |path: &Path| -> io::Result<(u64, u64)> {
        assert!(path.exists(), "probe {} must survive", path.display());
        Ok((100, 1000))
    };
    let mut report = Report::default();
    reclaim_below_floor(
        &repo,
        &roots,
        floor_3000(),
        &[],
        &[],
        (&Default::default(), &calm),
        (&low, &super::device),
        true,
        &mut report,
    )
    .unwrap();
    assert_eq!(removed(&targets), 2, "{report:?}");
    assert!(report.error.is_none());
}

#[test]
fn a_recovered_filesystem_stops_the_pass_even_when_the_estimate_is_short() {
    let (fixture, repo, root, _args) = pressure_fixture();
    let canonical = root.canonicalize().unwrap();
    let targets = ["tree-a", "tree-b"].map(|n| canonical.join(n).join("rust/target"));
    let roots = [canonical.clone(), fixture.path().canonicalize().unwrap()];
    // Floor 20000 is above what two 4098-byte targets could give by estimate, but the
    // filesystem reports 50000 free once anything has been removed.
    let thresholds = Thresholds {
        alarm_free_bytes: 1,
        cleanup_free_bytes: 20_000,
    };
    let calls = std::cell::Cell::new(0);
    let recovering = |path: &Path| -> io::Result<(u64, u64)> {
        assert!(path.exists());
        calls.set(calls.get() + 1);
        Ok((if calls.get() == 1 { 0 } else { 50_000 }, 1000))
    };
    let mut report = Report::default();
    reclaim_below_floor(
        &repo,
        &roots,
        thresholds,
        &[],
        &[],
        (&Default::default(), &calm),
        (&recovering, &super::device),
        true,
        &mut report,
    )
    .unwrap();
    let left = targets.iter().filter(|t| t.exists()).count();
    assert_eq!(
        left, 1,
        "one removal, then the measurement says recovered: {report:?}"
    );
}

#[test]
fn a_full_disk_with_no_candidate_still_fails_naming_that() {
    let (fixture, repo, root, _args) = pressure_fixture();
    let canonical = root.canonicalize().unwrap();
    for name in ["tree-a", "tree-b"] {
        std::fs::remove_dir_all(canonical.join(name).join("rust/target")).unwrap();
    }
    let full = |_: &Path| -> io::Result<(u64, u64)> { Ok((0, 1000)) };
    let mut report = Report::default();
    reclaim_below_floor(
        &repo,
        &[canonical, fixture.path().canonicalize().unwrap()],
        floor_3000(),
        &[],
        &[],
        (&Default::default(), &calm),
        (&full, &super::device),
        true,
        &mut report,
    )
    .unwrap();
    let error = report.error.expect("freed 0 below the floor is an error");
    assert!(error.contains("no Cargo target candidate"), "{error}");
    assert!(report.alarm);
}

#[test]
fn a_target_that_is_a_mount_point_is_measured_on_its_own_device() {
    let (fixture, repo, root, _args) = pressure_fixture();
    let canonical = root.canonicalize().unwrap();
    let targets = ["tree-a", "tree-b"].map(|n| canonical.join(n).join("rust/target"));
    // The targets are mount points of a small, full filesystem (device 2); everything above
    // them, their parents and the root, is on a roomy one (device 1).
    let device = |path: &Path| {
        Some(
            if path.starts_with(&canonical) && path.ends_with("target") {
                2
            } else {
                1
            },
        )
    };
    let probes = std::cell::RefCell::new(Vec::new());
    let space = |path: &Path| -> io::Result<(u64, u64)> {
        probes.borrow_mut().push(path.to_path_buf());
        Ok(if device(path) == Some(2) {
            (0, 1000)
        } else {
            (u64::MAX / 2, u64::MAX / 2)
        })
    };
    let mut report = Report::default();
    reclaim_below_floor(
        &repo,
        &[canonical.clone(), fixture.path().canonicalize().unwrap()],
        floor_3000(),
        &[],
        &[],
        (&Default::default(), &calm),
        (&space, &device),
        true,
        &mut report,
    )
    .unwrap();
    // The parent's free space (device 1) must not hide the target filesystem being full.
    assert!(
        probes.borrow().iter().any(|p| targets.contains(p)),
        "{:?}",
        probes.borrow()
    );
    assert!(targets.iter().all(|t| !t.exists()), "{report:?}");
}

#[test]
fn candidates_are_grouped_by_the_device_they_are_on() {
    let (fixture, repo, root, _args) = pressure_fixture();
    let canonical = root.canonicalize().unwrap();
    // Every probe reports the same full filesystem, so a group is built per real device: the
    // fixture lives on one, and its two targets are one group sampled by one probe path.
    let probes = std::cell::RefCell::new(Vec::new());
    let space = |path: &Path| -> io::Result<(u64, u64)> {
        probes.borrow_mut().push(path.to_path_buf());
        Ok((u64::MAX / 2, u64::MAX / 2))
    };
    let mut report = Report::default();
    reclaim_below_floor(
        &repo,
        &[canonical],
        Thresholds::default(),
        &[],
        &[],
        (&Default::default(), &calm),
        (&space, &super::device),
        true,
        &mut report,
    )
    .unwrap();
    assert_eq!(
        probes.borrow().len(),
        1,
        "one device, one measurement: {report:?}"
    );
    drop(fixture);
}

#[test]
fn the_default_cache_is_swept_even_when_the_primary_target_moved() {
    let home = Path::new("/fixture/homedir");
    let default = home.join(".cache/plangraph");
    assert_eq!(
        fast_check_caches(Some(Path::new("/data/ct/cargo-target")), home),
        [PathBuf::from("/data/ct"), default.clone()]
    );
    assert_eq!(
        fast_check_caches(Some(&default.join("cargo-target")), home),
        std::slice::from_ref(&default)
    );
    assert_eq!(fast_check_caches(None, home), [default]);
}
