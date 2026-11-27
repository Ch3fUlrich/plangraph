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
        projected: Default::default(),
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
        None,
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
        None,
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
            None,
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
        None,
        SystemTime::now(),
        true,
        &mut report,
        || panic!("pinned target"),
    )
    .unwrap();
    reclaim(
        &target,
        &guards,
        None,
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
        None,
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
        None,
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
        None,
        SystemTime::now(),
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
        SystemTime::now(),
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
        SystemTime::now(),
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
        SystemTime::now(),
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
        SystemTime::now(),
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
        SystemTime::now(),
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
        SystemTime::now(),
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

#[test]
#[cfg(target_os = "linux")]
fn affected_pytest_scratch_requires_owned_marker_age_and_idle_lease() {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};

    let temp = tempfile::tempdir().unwrap();
    let scratch = temp.path().canonicalize().unwrap();
    let now = SystemTime::UNIX_EPOCH + Duration::from_secs(86_400);
    let make = |name: &str, marker: &[u8], age: u64| {
        let root = scratch.join(name);
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        let artifact = root.join("pytest-artifact");
        let lease = root.join(".lease");
        fs::write(&artifact, b"fixture").unwrap();
        fs::write(&lease, marker).unwrap();
        for path in [&root, &artifact, &lease] {
            fs::File::open(path)
                .unwrap()
                .set_modified(now - Duration::from_secs(age))
                .unwrap();
        }
        root
    };
    let marker = b"plangraph-affected-pytest-v1\n";
    let stale = make("pg-affected-stale", marker, 1801);
    let active = make("pg-affected-active", marker, 1801);
    let recent = make("pg-affected-recent", marker, 1800);
    let unrelated = make("pg-affected-unrelated", b"unrelated", 1801);
    let public = make("pg-affected-public", marker, 1801);
    fs::set_permissions(&public, fs::Permissions::from_mode(0o755)).unwrap();

    let active_lease = crate::cargo_cache::CacheLease::try_exclusive_file(
        fs::File::open(active.join(".lease")).unwrap(),
    )
    .unwrap()
    .unwrap();
    // A fork can retain this open file description until its child execs.
    let inherited = active_lease.file().try_clone().unwrap();

    let mut result = report();
    let sweep = |result: &mut Report| {
        sweep_using(
            Some(&scratch),
            &[],
            &Guards::default(),
            now,
            true,
            result,
            || Ok((vec![], false)),
        )
        .unwrap();
    };
    sweep(&mut result);
    assert!(!stale.exists());
    assert!(active.exists());
    assert_eq!(
        result.skipped,
        [format!("{}: pytest lease held", active.display())]
    );
    drop(active_lease);
    sweep(&mut result);
    assert!(!active.exists());
    assert!(recent.exists() && unrelated.exists() && public.exists());
    drop(inherited);
}

#[cfg(target_os = "linux")]
#[test]
fn hygiene_generic_cache_probe_refuses_committed_artifacts() {
    use crate::disk_hygiene::{Candidate, Layout};
    use std::time::Instant;
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path();
    let git = |args: &[&str]| {
        crate::host_command::output(
            crate::git_test_support::command(repo)
                .args([
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                ])
                .args(args),
            Duration::from_secs(10),
        )
        .unwrap()
    };
    git(&["init", "-q"]);
    fs::write(repo.join(".gitignore"), "target/\n").unwrap();
    let target = repo.join("target");
    fs::create_dir(&target).unwrap();
    fs::write(target.join("checked-in"), "preserve this file").unwrap();
    git(&["add", ".gitignore"]);
    git(&["add", "-f", "target/checked-in"]);
    git(&["commit", "-qm", "fixture"]);
    assert!(git(&["status", "--porcelain"]).is_empty());
    let mut item = Candidate::new(target, repo.to_path_buf(), SystemTime::now()).unwrap();
    item.scope = repo.to_path_buf();
    let layout = Layout {
        repo,
        roots: &[],
        scratch: &[],
        shared: &[],
        home: repo,
        cap: 0,
    };
    assert_eq!(
        layout
            .protection(&item, repo, Instant::now() + Duration::from_secs(3))
            .unwrap(),
        Some("unsafe")
    );
}

#[cfg(target_os = "linux")]
#[test]
fn selector_scratch_uses_its_run_lease_for_final_proof() {
    use crate::{cargo_cache::CacheLease, disk_hygiene::FinalProof};
    use std::os::unix::fs::PermissionsExt;

    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("pg-affected-fixture");
    fs::create_dir(&target).unwrap();
    fs::set_permissions(&target, fs::Permissions::from_mode(0o700)).unwrap();
    let marker = target.join(".lease");
    fs::write(&marker, b"plangraph-affected-pytest-v1\n").unwrap();
    let now = SystemTime::now();
    for path in [&target, &marker] {
        fs::File::open(path)
            .unwrap()
            .set_modified(now - Duration::from_secs(3600))
            .unwrap();
    }
    let mut discovered = crate::affected_scratch::discover(
        tmp.path(),
        &[],
        &mut || true,
        now,
        crate::affected_scratch::is_run,
    )
    .unwrap();
    assert_eq!(discovered.len(), 1);
    let item = discovered.pop().unwrap();
    assert_eq!(item.path, target);
    let held = CacheLease::try_exclusive_file(fs::File::open(&marker).unwrap()).unwrap();
    assert!(held.is_some());
    assert_eq!(FinalProof::begin(item.clone()).err(), Some("leased"));
    drop(held);
    let mut proof = FinalProof::begin(item).unwrap();
    assert!(proof.advance(|| true).unwrap());
    assert!(
        CacheLease::try_exclusive_file(fs::File::open(&marker).unwrap())
            .unwrap()
            .is_none()
    );
    assert!(!tmp
        .path()
        .join("pg-affected-fixture.plangraph-lock")
        .exists());
    assert!(proof.remove(true, || Ok(true), || true).unwrap().is_some());
    assert!(!target.exists());
}

#[cfg(target_os = "linux")]
#[test]
fn cap_scan_reports_an_incomplete_total_at_the_deadline() {
    use crate::{
        cargo_pool::hygiene_cap_failure,
        disk_hygiene::{Candidate, Projection},
        shared_target::PoolCap,
    };
    use std::cell::Cell;

    let tmp = tempfile::tempdir().unwrap();
    let slot = tmp.path().join("slot");
    fs::create_dir(&slot).unwrap();
    fs::write(slot.join("data"), [0u8; 128]).unwrap();
    let mut item =
        Candidate::new(slot.clone(), tmp.path().to_path_buf(), SystemTime::now()).unwrap();
    let slots = vec![slot].into();
    let projected = Projection::default();
    item.cap = Some(PoolCap(1024, 1024, slots));
    let permit = Cell::new(true);
    assert_eq!(
        hygiene_cap_failure(&item, &projected, || permit.replace(false)),
        Some("run_budget")
    );
    assert_eq!(
        hygiene_cap_failure(&item, &projected, || true),
        Some("cap_met")
    );
    let Some(PoolCap(limit, _, _)) = item.cap.as_mut() else {
        unreachable!()
    };
    *limit = 64;
    assert_eq!(hygiene_cap_failure(&item, &projected, || true), None);
}
