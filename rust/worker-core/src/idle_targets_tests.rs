use super::*;
use std::fs;
use std::path::Path;

fn target(container: &Path, name: &str, age: Duration) -> PathBuf {
    let path = container.join(name);
    fs::create_dir_all(path.join("debug")).unwrap();
    fs::write(path.join(".rustc_info.json"), b"{}").unwrap();
    fs::write(path.join("debug/blob"), [0u8; 64]).unwrap();
    let old = SystemTime::now() - age;
    for p in [
        path.join("debug/blob"),
        path.join(".rustc_info.json"),
        path.join("debug"),
        path.clone(),
    ] {
        fs::File::open(p).unwrap().set_modified(old).unwrap();
    }
    path
}

#[test]
fn hygiene_discovery_finds_nested_targets_and_excludes_every_pool() {
    use std::os::unix::fs::{symlink, PermissionsExt};

    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path().canonicalize().unwrap();
    let cache = home.join(".cache");
    let age = Duration::from_secs(3600);
    let nested = target(&cache, "session/target", age);
    let deep = target(&cache, "other/level/target", age);
    let shared = [
        target(&cache, "shared-a", age),
        target(&cache, "shared-b", age),
    ];
    let pools = shared
        .each_ref()
        .map(|root| crate::cargo_pool::pool_root(root).unwrap());
    for pool in &pools {
        target(pool, "slot-1", age);
    }
    symlink(&pools[1], cache.join("alias-of-pool")).unwrap();
    let lookalike = cache.join("not-a-cache");
    fs::create_dir_all(lookalike.join("debug")).unwrap();
    let unreadable = cache.join("unreadable");
    fs::create_dir(&unreadable).unwrap();
    let permissions = unreadable.metadata().unwrap().permissions();
    fs::set_permissions(&unreadable, fs::Permissions::from_mode(0o000)).unwrap();
    let denied = unreadable.read_dir().is_err();
    symlink(
        nested.join(".rustc_info.json"),
        lookalike.join(".rustc_info.json"),
    )
    .unwrap();
    let excluded = crate::cargo_pool::hygiene_exclusions(&shared, &home);
    let mut kept = vec![];
    let result = targets_in(
        &cache,
        2,
        &excluded,
        || true,
        |p, why| kept.push((p.to_path_buf(), why)),
    );
    fs::set_permissions(&unreadable, permissions).unwrap();
    let (found, complete) = result.unwrap();
    if denied {
        assert!(kept.contains(&(unreadable, "probe_unavailable")));
    }
    assert!(complete);
    assert_eq!(found, [nested.as_path()]);
    assert!(deep.is_dir() && pools.iter().all(|pool| pool.join("slot-1").is_dir()));

    let mut budget = 2usize;
    let (_, complete) = targets_in(
        &cache,
        2,
        &excluded,
        || {
            let room = budget != 0;
            budget = budget.saturating_sub(1);
            room
        },
        |_, _| {},
    )
    .unwrap();
    assert!(!complete);
    assert!(nested.is_dir());
}

fn run(containers: &[PathBuf], guards: &Guards, apply: bool, building: bool) -> Report {
    let mut report = Report::default();
    sweep(
        containers,
        guards,
        SystemTime::now(),
        apply,
        &mut report,
        || Ok((vec![], building)),
    )
    .unwrap();
    report
}

#[test]
fn only_targets_idle_for_a_day_are_removed_and_the_container_stays() {
    let tmp = tempfile::tempdir().unwrap();
    let container = tmp.path().join("cargo-target");
    let old = target(&container, "batch23", Duration::from_secs(3 * 86400));
    let recent = target(&container, "shared", Duration::from_secs(3600));
    fs::create_dir_all(container.join("notes")).unwrap();
    let dry = run(
        std::slice::from_ref(&container),
        &Guards::default(),
        false,
        false,
    );
    assert!(dry.actions.iter().any(|a| a.starts_with("would-remove")));
    assert!(old.exists());
    let applied = run(
        std::slice::from_ref(&container),
        &Guards::default(),
        true,
        false,
    );
    assert!(applied.freed_bytes > 0);
    assert!(!old.exists() && recent.exists() && container.join("notes").exists());
}

#[test]
fn a_running_build_a_pinned_or_a_plain_directory_keeps_its_target() {
    let tmp = tempfile::tempdir().unwrap();
    let container = tmp.path().join("cargo-target");
    let old = target(&container, "batch24", Duration::from_secs(3 * 86400));
    assert_eq!(
        run(
            std::slice::from_ref(&container),
            &Guards::default(),
            true,
            true
        )
        .freed_bytes,
        0
    );
    assert!(old.exists());
    let pinned = Guards {
        pinned: vec![old.canonicalize().unwrap()],
        live: vec![],
    };
    assert_eq!(
        run(std::slice::from_ref(&container), &pinned, true, false).freed_bytes,
        0
    );
    assert!(old.exists());
    // Without cargo's markers a directory is not a target, whatever it is named or how old it is.
    let plain = container.join("project");
    fs::create_dir_all(plain.join("debug")).unwrap();
    let ancient = SystemTime::now() - Duration::from_secs(30 * 86400);
    fs::File::open(plain.join("debug"))
        .unwrap()
        .set_modified(ancient)
        .unwrap();
    fs::File::open(&plain)
        .unwrap()
        .set_modified(ancient)
        .unwrap();
    run(&[container], &Guards::default(), true, false);
    assert!(plain.join("debug").exists());
}

#[test]
fn a_container_that_is_itself_a_target_and_a_missing_one_are_handled() {
    let tmp = tempfile::tempdir().unwrap();
    let whole = tmp.path().join("pg-target-warm");
    fs::create_dir_all(&whole).unwrap();
    let inner = target(tmp.path(), "pg-target-warm", Duration::from_secs(5 * 86400));
    assert_eq!(inner, whole);
    let missing = tmp.path().join("absent");
    let report = run(&[missing, whole.clone()], &Guards::default(), true, false);
    assert!(report.freed_bytes > 0 && !whole.exists());
}

#[test]
fn a_home_reached_through_a_symlinked_ancestor_is_still_swept_but_a_linked_target_is_not() {
    let tmp = tempfile::tempdir().unwrap();
    let real = tmp.path().join("real");
    let container = real.join("cargo-target");
    let old = target(&container, "batch23", Duration::from_secs(3 * 86400));
    let outside = target(
        &tmp.path().join("elsewhere"),
        "kept",
        Duration::from_secs(3 * 86400),
    );
    std::os::unix::fs::symlink(&outside, container.join("linked")).unwrap();
    let link = tmp.path().join("link");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let report = run(
        &[link.join("cargo-target")],
        &Guards::default(),
        true,
        false,
    );
    assert!(report.freed_bytes > 0 && !old.exists());
    assert!(outside.exists() && container.join("linked").exists());
}

#[test]
fn a_held_cache_lease_keeps_an_idle_legacy_target() {
    let tmp = tempfile::tempdir().unwrap();
    let container = tmp.path().join("cargo-target");
    let old = target(&container, "batch23", Duration::from_secs(3 * 86400));
    let lease = crate::cargo_cache::try_exclusive_lease(&old)
        .unwrap()
        .unwrap();
    let held = run(
        std::slice::from_ref(&container),
        &Guards::default(),
        true,
        false,
    );
    assert_eq!(held.freed_bytes, 0);
    assert!(old.exists());
    assert!(held.skipped.iter().any(|s| s.contains("cache lease held")));
    drop(lease);
    assert!(run(&[container], &Guards::default(), true, false).freed_bytes > 0);
    assert!(!old.exists());
}

fn age_all(path: &Path) {
    let old = SystemTime::now() - Duration::from_secs(3 * 86400);
    let mut pending = vec![path.to_path_buf()];
    while let Some(entry) = pending.pop() {
        if fs::symlink_metadata(&entry).unwrap().is_dir() {
            pending.extend(fs::read_dir(&entry).unwrap().map(|e| e.unwrap().path()));
        }
        fs::File::open(&entry).unwrap().set_modified(old).unwrap();
    }
}

#[test]
fn a_git_repository_below_a_legacy_target_keeps_it() {
    let tmp = tempfile::tempdir().unwrap();
    let container = tmp.path().join("cargo-target");
    let sweep = |container: &Path| {
        run(
            std::slice::from_ref(&container.to_path_buf()),
            &Guards::default(),
            true,
            false,
        )
    };
    // A checkout, a linked worktree's `.git` file, and a bare repository (HEAD beside objects and refs).
    for shape in ["checkout", "linked", "bare"] {
        let old = target(&container, "batch23", Duration::from_secs(3 * 86400));
        let repository = old.join("scratch");
        match shape {
            "checkout" => fs::create_dir_all(repository.join(".git")).unwrap(),
            "linked" => {
                fs::create_dir_all(&repository).unwrap();
                fs::write(repository.join(".git"), "gitdir: elsewhere\n").unwrap();
            }
            _ => {
                fs::create_dir_all(repository.join("objects")).unwrap();
                fs::create_dir_all(repository.join("refs")).unwrap();
                fs::write(repository.join("HEAD"), "ref: refs/heads/main\n").unwrap();
            }
        }
        age_all(&old);
        let report = sweep(&container);
        assert_eq!(report.freed_bytes, 0, "{shape}");
        assert!(repository.exists(), "{shape}");
        assert!(
            report.skipped.iter().any(|s| s.contains("git repository")),
            "{shape}"
        );
        // Without the repository the same aged target goes.
        fs::remove_dir_all(&repository).unwrap();
        age_all(&old);
        assert!(sweep(&container).freed_bytes > 0, "{shape}");
        assert!(!old.exists(), "{shape}");
    }
}

#[test]
fn a_leased_target_with_an_unreadable_child_does_not_stop_the_sweep_of_its_sibling() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let container = tmp.path().join("cargo-target");
    let leased = target(&container, "batch23", Duration::from_secs(3 * 86400));
    let sibling = target(&container, "batch24", Duration::from_secs(3 * 86400));
    let hidden = leased.join("hidden");
    fs::create_dir_all(&hidden).unwrap();
    fs::set_permissions(&hidden, fs::Permissions::from_mode(0o000)).unwrap();
    let lease = crate::cargo_cache::try_exclusive_lease(&leased)
        .unwrap()
        .unwrap();
    let report = run(
        std::slice::from_ref(&container),
        &Guards::default(),
        true,
        false,
    );
    drop(lease);
    fs::set_permissions(&hidden, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(
        leased.exists() && !sibling.exists(),
        "{:?} {:?}",
        report.skipped,
        report.actions
    );
    assert!(report
        .skipped
        .iter()
        .any(|s| s.contains("cache lease held")));
}

#[cfg(target_os = "linux")]
#[test]
fn truncated_ranking_refreshes_and_restarts_changed_metadata() {
    use crate::disk_hygiene::{rank_candidates, refresh_ranking, Budget, Candidate};
    use std::time::Instant;
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let now = SystemTime::now();
    let candidates = ["stable", "changed"].map(|name| {
        let path = target(&root, name, Duration::from_secs(3600));
        Candidate::new(path, root.clone(), now).unwrap()
    });
    let mut budget = Budget::new(Instant::now(), &Instant::now);
    budget.candidate_entries = 4;
    let mut rows = rank_candidates(candidates.into(), &budget);
    assert!(rows.iter().all(|(_, row)| !row.complete()));
    fs::File::open(root.join("changed/debug/blob"))
        .unwrap()
        .set_modified(now)
        .unwrap();
    refresh_ranking(&mut rows, &budget);
    assert!(rows[0].1.complete());
    assert_eq!(rows[1].1.reason, Some("young"));
}
