use super::*;
#[test]
fn disk_hygiene_denied_cwd_needs_authoritative_reader() {
    let rows = resolve_processes(
        Err(io::Error::from(io::ErrorKind::PermissionDenied)),
        || Ok((vec!["/protected/tree".into()], false)),
    )
    .unwrap();
    assert_eq!(rows.0, vec![std::path::PathBuf::from("/protected/tree")]);
    assert!(resolve_processes(
        Err(io::Error::from(io::ErrorKind::PermissionDenied)),
        || Err(io::Error::other("reader unavailable"))
    )
    .is_err());
    let rows = resolve_processes(Ok((vec![], false)), || panic!("reader should not run")).unwrap();
    assert!(rows.0.is_empty());
}
#[test]
#[cfg(unix)]
fn disk_hygiene_shared_cache_apply_rechecks_builds_cwds_and_symlinks() {
    let tmp = tempfile::tempdir().unwrap();
    let cache = tmp.path().join("cargo-target");
    fs::create_dir(&cache).unwrap();
    let object = cache.join("debug/incremental/session");
    fs::create_dir_all(&object).unwrap();
    fs::write(object.join("object"), [0u8; 42]).unwrap();
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(90000);
    for p in [object.join("object"), object.clone()] {
        fs::File::open(p).unwrap().set_modified(old).unwrap();
    }
    let mut report = Report {
        free_bytes: 4_000_000_000,
        alarm: true,
        dry_run: false,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    };
    shared_cache_using(
        &cache,
        &Guards::default(),
        false,
        0,
        true,
        false,
        &mut report,
        || Ok((vec![cache.clone()], false)),
    )
    .unwrap();
    assert!(report.actions.is_empty());
    let mut probes = 0;
    shared_cache_using(
        &cache,
        &Guards::default(),
        false,
        0,
        true,
        true,
        &mut report,
        || {
            probes += 1;
            Ok((vec![], probes == 2))
        },
    )
    .unwrap();
    assert!(object.join("object").exists());
    assert_eq!(report.freed_bytes, 0);
    shared_cache_using(
        &cache,
        &Guards::default(),
        false,
        0,
        true,
        true,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(cache.exists() && !object.exists());
    assert_eq!(report.freed_bytes, 42);
    #[cfg(unix)]
    {
        fs::remove_dir_all(&cache).unwrap();
        std::os::unix::fs::symlink(tmp.path().join("absent"), &cache).unwrap();
        assert!(shared_cache_using(
            &cache,
            &Guards::default(),
            false,
            0,
            true,
            true,
            &mut report,
            || panic!("symlink must be refused")
        )
        .is_err());
    }
}

#[test]
#[cfg(unix)]
fn disk_hygiene_process_inventory_reads_builds_and_cwds() {
    let tmp = tempfile::tempdir().unwrap();
    let pid = tmp.path().join("123");
    fs::create_dir(&pid).unwrap();
    fs::write(pid.join("comm"), "cargo\n").unwrap();
    std::os::unix::fs::symlink("/protected/tree", pid.join("cwd")).unwrap();
    let (cwds, building) = scan_process_paths(tmp.path(), false).unwrap();
    assert!(building);
    assert_eq!(cwds, vec![std::path::PathBuf::from("/protected/tree")]);
    fs::remove_file(pid.join("cwd")).unwrap();
    fs::write(pid.join("comm"), "node\n").unwrap();
    let (cwds, building) = scan_process_paths(tmp.path(), false).unwrap();
    assert!(!building);
    assert!(cwds.is_empty());
    fs::create_dir(pid.join("cwd")).unwrap();
    assert!(scan_process_paths(tmp.path(), false).is_err());
}
#[test]
#[cfg(target_os = "linux")]
fn disk_hygiene_denied_foreign_cwd_cannot_be_ignored() {
    use std::os::unix::fs::MetadataExt;
    // A real root-owned proc entry models the inode-owner mismatch of
    // non-dumpable processes without needing root/chown in the fixture.
    let probe = fs::read_link("/proc/1/cwd");
    if probe.is_ok() || fs::metadata("/proc/1").unwrap().uid() == unsafe { libc::geteuid() } {
        return;
    } // No foreign denied entry on privileged runners.
    assert_eq!(probe.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
    let tmp = tempfile::tempdir().unwrap();
    std::os::unix::fs::symlink("/proc/1", tmp.path().join("1")).unwrap();
    let result = scan_process_paths(tmp.path(), false);
    assert!(
        result.is_err(),
        "denied foreign cwd must not disappear from inventory"
    );
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
}

#[test]
#[cfg(unix)]
fn shared_cap_evicts_old_build_variants_and_rechecks_live_cargo() {
    let tmp = tempfile::tempdir().unwrap();
    // Both the cache and its sibling lease belong to this fixture.
    let cache = &tmp.path().join("cargo-target");
    let old = cache.join("debug/build/my-crate-1111111111111111");
    let new = cache.join("debug/build/my-crate-2222222222222222");
    let old_compile = cache.join("debug/build/my-crate-3333333333333333");
    let new_compile = cache.join("debug/build/my-crate-4444444444444444");
    for (path, age, marker) in [
        (&old, 90000, "output"),
        (&new, 80000, "output"),
        (&old_compile, 100000, "build-script-build"),
        (&new_compile, 85000, "build-script-build"),
    ] {
        fs::create_dir_all(path).unwrap();
        fs::write(path.join(marker), [0u8; 42]).unwrap();
        let modified = std::time::SystemTime::now() - std::time::Duration::from_secs(age);
        for p in [path.join(marker), path.clone()] {
            fs::File::open(p).unwrap().set_modified(modified).unwrap();
        }
    }
    let mut report = Report {
        free_bytes: 20_000_000_000,
        alarm: false,
        dry_run: true,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    };
    shared_cache_using(
        cache,
        &Guards::default(),
        false,
        200,
        false,
        false,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(report.actions.is_empty()); // Under cap, no pressure.
    shared_cache_using(
        cache,
        &Guards::default(),
        false,
        84,
        false,
        false,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert_eq!(report.actions.len(), 2);
    assert!(old.exists() && new.exists() && old_compile.exists() && new_compile.exists());
    report.actions.clear();
    let mut probes = 0;
    shared_cache_using(
        cache,
        &Guards::default(),
        false,
        84,
        false,
        true,
        &mut report,
        || {
            probes += 1;
            Ok((vec![], probes == 2)) // Cargo starts with cwd outside the target.
        },
    )
    .unwrap();
    assert!(old.exists() && new.exists() && old_compile.exists() && new_compile.exists());
    assert!(report.actions.is_empty());
    shared_cache_using(
        cache,
        &Guards::default(),
        false,
        84,
        false,
        true,
        &mut report,
        || Ok((vec![], false)),
    )
    .unwrap();
    assert!(!old.exists() && new.exists() && !old_compile.exists() && new_compile.exists());
    assert_eq!(report.freed_bytes, 84);
    assert_eq!(report.actions.len(), 2);
}

#[test]
#[cfg(unix)]
fn shared_cache_honors_run_and_cleanup_leases_through_reclaim() {
    use std::os::fd::AsRawFd;
    let tmp = tempfile::tempdir().unwrap();
    let cache = tmp.path().join("cargo-target");
    let candidate = cache.join("debug/incremental/old-session");
    fs::create_dir_all(&candidate).unwrap();
    let object = candidate.join("object");
    fs::write(&object, [0u8; 42]).unwrap();
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(90000);
    for path in [&object, &candidate] {
        fs::File::open(path).unwrap().set_modified(old).unwrap();
    }
    // Use host_ready.py's literal sibling name and flock API independently
    // of the production Rust lock helper, including an idle pre-spawn lease.
    let lock = fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(tmp.path().join("cargo-target.plangraph-lock"))
        .unwrap();
    let mut report = Report {
        free_bytes: 0,
        alarm: true,
        dry_run: false,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    };
    for mode in [libc::LOCK_SH, libc::LOCK_EX] {
        // SAFETY: lock owns the descriptor.
        assert_eq!(
            unsafe { libc::flock(lock.as_raw_fd(), mode | libc::LOCK_NB) },
            0
        );
        for apply in [false, true] {
            shared_cache_using(
                &cache,
                &Guards::default(),
                false,
                0,
                true,
                apply,
                &mut report,
                || panic!("held lease must skip before probing"),
            )
            .unwrap();
            assert!(object.exists());
            assert!(report.actions.is_empty());
            assert_eq!(report.freed_bytes, 0);
            assert!(report.skipped.last().unwrap().contains("cache lease held"));
        }
        // SAFETY: lock owns the descriptor.
        assert_eq!(unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_UN) }, 0);
    }
    let mut probes = 0;
    shared_cache_using(
        &cache,
        &Guards::default(),
        false,
        0,
        true,
        true,
        &mut report,
        || {
            probes += 1;
            // A run cannot acquire a shared lease while hygiene owns reclaim.
            // SAFETY: lock owns the descriptor.
            assert_eq!(
                unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) },
                -1
            );
            assert_eq!(io::Error::last_os_error().kind(), io::ErrorKind::WouldBlock);
            Ok((vec![], false))
        },
    )
    .unwrap();
    assert_eq!(probes, 2);
    assert!(!candidate.exists());
    assert_eq!(report.freed_bytes, 42);
    assert!(crate::cargo_cache::try_exclusive_lease(&cache)
        .unwrap()
        .is_some());
    assert!(tmp.path().join("cargo-target.plangraph-lock").exists());
}

#[test]
#[cfg(unix)]
fn reaper_inventory_includes_open_files_and_executables() {
    use std::{os::unix::fs::symlink, path::PathBuf};
    let tmp = tempfile::tempdir().unwrap();
    let pid = tmp.path().join("42");
    fs::create_dir_all(pid.join("fd")).unwrap();
    fs::write(pid.join("comm"), "example").unwrap();
    symlink("/", pid.join("cwd")).unwrap();
    symlink("/candidate/program", pid.join("exe")).unwrap();
    symlink("/candidate/file", pid.join("fd/4")).unwrap();
    symlink("pipe:[123]", pid.join("fd/5")).unwrap();
    let (paths, _) = scan_process_paths(tmp.path(), true).unwrap();
    assert!(paths.contains(&PathBuf::from("/candidate/program")));
    assert!(paths.contains(&PathBuf::from("/candidate/file")));
    assert!(paths.iter().all(|p| p.is_absolute()));
}

#[test]
#[cfg(target_os = "linux")]
fn post_run_caps_identified_debug_only_when_idle() {
    let parent = tempfile::tempdir().unwrap();
    let checkout = parent.path().join("checkout");
    let target = parent.path().join("target");
    let profile = target.join("debug");
    let sibling = target.join("unrelated");
    fs::create_dir_all(&profile).unwrap();
    fs::create_dir(&sibling).unwrap();
    fs::create_dir(&checkout).unwrap();
    fs::write(profile.join(".cargo-lock"), []).unwrap();
    fs::write(profile.join("new-artifact"), [0; 100]).unwrap();
    fs::write(sibling.join("data"), [0; 12]).unwrap();
    fs::write(
        target.join("CACHEDIR.TAG"),
        b"Signature: 8a477f597d28d172789f06886806bc55",
    )
    .unwrap();

    let run =
        |building| post_run_cargo_using(&checkout, &target, 80, || Ok((vec![], building))).unwrap();
    let lease = crate::cargo_cache::try_exclusive_lease(&target)
        .unwrap()
        .unwrap();
    assert!(run(false).actions.is_empty());
    drop(lease);
    assert!(run(true).actions.is_empty());
    assert!(profile.exists());

    assert_eq!(run(false).freed_bytes, 100);
    assert!(crate::disk_hygiene::scan(&target).unwrap().0 <= 80);
    assert!(sibling.join("data").exists());
}

#[test]
#[cfg(target_os = "linux")]
fn post_run_trims_fifty_stale_build_variants_under_the_watermark_keeping_live_ones() {
    let parent = tempfile::tempdir().unwrap();
    let checkout = parent.path().join("checkout");
    let target = parent.path().join("target");
    let profile = target.join("debug");
    fs::create_dir_all(profile.join("build")).unwrap();
    fs::create_dir(&checkout).unwrap();
    fs::write(profile.join(".cargo-lock"), []).unwrap();
    fs::write(
        target.join("CACHEDIR.TAG"),
        b"Signature: 8a477f597d28d172789f06886806bc55",
    )
    .unwrap();
    let now = std::time::SystemTime::now();
    let unit = |name: &str, age_minutes: u64| {
        let path = profile.join("build").join(name);
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("build-script-build"), vec![0u8; 100]).unwrap();
        let modified = now - std::time::Duration::from_secs(age_minutes * 60);
        for p in [path.join("build-script-build"), path.clone()] {
            fs::File::open(p).unwrap().set_modified(modified).unwrap();
        }
        path
    };
    let stale: Vec<_> = (0..50)
        .map(|n| unit(&format!("pg-worker-core-{n:016x}"), 361 + n))
        .collect();
    // Held by a running build: touched minutes ago, and the crate's newest variant.
    let live = unit("pg-worker-core-ffffffffffffffff", 5);
    let other = unit("other-crate-0000000000000001", 390);
    // 52 units of 100 B; cap 5200 leaves the watermark at 3900 B.
    let cap = 5200;
    assert_eq!(crate::cargo_pool::watermark_bytes(cap), 3900);
    let size = || crate::disk_hygiene::scan(&target).unwrap().0;
    assert!(size() > cap);
    let report = post_run_cargo_using(&checkout, &target, cap, || Ok((vec![], false))).unwrap();
    assert!(
        size() <= crate::cargo_pool::watermark_bytes(cap),
        "{}",
        size()
    );
    // Oldest first: the freshest stale variants survive, the oldest are gone.
    assert!(!stale[49].exists() && !stale[48].exists());
    assert!(live.exists() && other.exists());
    assert!(
        profile.join("build").exists(),
        "debug profile kept under the cap"
    );
    assert!(
        report.freed_bytes >= 1300
            && report
                .actions
                .iter()
                .all(|a| !a.contains("ffffffffffffffff"))
    );
}

#[cfg(target_os = "linux")]
fn over_cap_target(
    parent: &Path,
) -> (
    std::path::PathBuf,
    std::path::PathBuf,
    Vec<std::path::PathBuf>,
) {
    let checkout = parent.join("checkout");
    let target = parent.join("target");
    let profile = target.join("debug");
    fs::create_dir_all(profile.join("build")).unwrap();
    fs::create_dir(&checkout).unwrap();
    fs::write(profile.join(".cargo-lock"), []).unwrap();
    fs::write(
        target.join("CACHEDIR.TAG"),
        b"Signature: 8a477f597d28d172789f06886806bc55",
    )
    .unwrap();
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(7 * 3600);
    let units = (0..10)
        .map(|n| {
            let path = profile.join("build").join(format!("pg-core-{n:016x}"));
            fs::create_dir_all(&path).unwrap();
            fs::write(path.join("build-script-build"), vec![0u8; 100]).unwrap();
            for p in [path.join("build-script-build"), path.clone()] {
                fs::File::open(p).unwrap().set_modified(old).unwrap();
            }
            path
        })
        .collect();
    (checkout, target, units)
}

#[test]
#[cfg(target_os = "linux")]
fn trim_refusals_name_the_live_holder_pid() {
    let parent = tempfile::tempdir().unwrap();
    let (checkout, target, _) = over_cap_target(parent.path());
    let lease = crate::cargo_cache::writer_lease(&target).unwrap();
    let report = post_run_cargo_using(&checkout, &target, 700, || Ok((vec![], false))).unwrap();
    let note = format!("held by pid {}", std::process::id());
    assert!(
        report
            .skipped
            .iter()
            .any(|s| s.contains("cache lease held") && s.contains(&note)),
        "{:?}",
        report.skipped
    );
    drop(lease);
    // A pool slot over its cap and leased reports the same holder.
    let slot = parent.path().join("slot-0");
    fs::create_dir_all(slot.join("debug")).unwrap();
    fs::write(slot.join("debug/blob"), [0u8; 64]).unwrap();
    let _slot_lease = crate::cargo_cache::writer_lease(&slot).unwrap();
    let mut report = Report::default();
    crate::cargo_pool::enforce_pool_cap(&[slot], 10, true, &mut report).unwrap();
    assert!(
        report
            .skipped
            .iter()
            .any(|s| s.contains("(leased") && s.contains(&note)),
        "{:?}",
        report.skipped
    );
}

/// Half of what the fixture occupies on disk: over the cap before, under it once the units go.
#[cfg(target_os = "linux")]
fn half_allocated(target: &Path) -> u64 {
    crate::disk_measure::allocated_bytes(target).unwrap() / 2
}

#[test]
#[cfg(target_os = "linux")]
fn push_reclaim_brings_an_over_cap_target_under_the_cap_under_the_exclusive_lease() {
    let parent = tempfile::tempdir().unwrap();
    let (checkout, target, _) = over_cap_target(parent.path());
    let cap = half_allocated(&target);
    assert!(crate::disk_measure::allocated_bytes(&target).unwrap() > cap);
    let report =
        reclaim_before_push_using(&checkout, &target, cap, || Ok((vec![], false))).unwrap();
    assert!(report.freed_bytes > 0);
    assert!(crate::disk_measure::allocated_bytes(&target).unwrap() <= cap);
    // Under the cap nothing is touched, whatever is running.
    let kept = reclaim_before_push_using(&checkout, &target, cap, || Ok((vec![], true))).unwrap();
    assert!(kept.actions.is_empty() && kept.freed_bytes == 0);
}

#[test]
#[cfg(target_os = "linux")]
fn push_reclaim_refuses_a_target_with_a_real_git_directory_and_keeps_it() {
    let parent = tempfile::tempdir().unwrap();
    let (checkout, target, units) = over_cap_target(parent.path());
    let cap = half_allocated(&target);
    fs::create_dir(target.join(".git")).unwrap();
    let error =
        reclaim_before_push_using(&checkout, &target, cap, || Ok((vec![], false))).unwrap_err();
    assert!(error.starts_with("cargo_target_not_reclaimed:"), "{error}");
    assert!(
        error.contains(".git") && error.contains("retry the push"),
        "{error}"
    );
    assert!(units.iter().all(|unit| unit.exists()));
}

#[test]
#[cfg(target_os = "linux")]
fn push_reclaim_leaves_a_receipt_while_a_second_build_runs_or_a_lease_is_held() {
    let parent = tempfile::tempdir().unwrap();
    let (checkout, target, units) = over_cap_target(parent.path());
    let cap = half_allocated(&target);
    let error =
        reclaim_before_push_using(&checkout, &target, cap, || Ok((vec![], true))).unwrap_err();
    assert!(
        error.contains("still holds") && error.contains("retry the push"),
        "{error}"
    );
    assert!(units.iter().all(|unit| unit.exists()));
    let lease = crate::cargo_cache::writer_lease(&target).unwrap();
    let error =
        reclaim_before_push_using(&checkout, &target, cap, || Ok((vec![], false))).unwrap_err();
    assert!(
        error.contains(&format!("held by pid {}", std::process::id())),
        "{error}"
    );
    assert!(error.contains("cache lease held"), "{error}");
    assert!(units.iter().all(|unit| unit.exists()));
    drop(lease);
    assert!(reclaim_before_push_using(&checkout, &target, cap, || Ok((vec![], false))).is_ok());
}

#[test]
#[cfg(target_os = "linux")]
fn push_reclaim_follows_a_symlinked_target_to_its_real_directory() {
    let parent = tempfile::tempdir().unwrap();
    let (checkout, real, units) = over_cap_target(parent.path());
    let cap = half_allocated(&real);
    let link = parent.path().join("configured-target");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    // Held at the real path (as a writer's shim holds it): the receipt names the holder.
    let command = crate::cargo_cache::writer_lease(&real).unwrap();
    let error =
        reclaim_before_push_using(&checkout, &link, cap, || Ok((vec![], false))).unwrap_err();
    assert!(
        error.contains("not_reclaimed") && error.contains("held by pid"),
        "{error}"
    );
    assert!(units.iter().all(|unit| unit.exists()));
    drop(command);
    reclaim_before_push_using(&checkout, &link, cap, || Ok((vec![], false))).unwrap();
    assert!(crate::disk_measure::allocated_bytes(&real).unwrap() <= cap);
    // A target that is not cargo output is refused through the link too, never counted as empty.
    let (_, other, _) = over_cap_target(&parent.path().join("second"));
    let other_link = parent.path().join("other-link");
    std::os::unix::fs::symlink(&other, &other_link).unwrap();
    fs::create_dir(other.join(".git")).unwrap();
    let error =
        reclaim_before_push_using(&checkout, &other_link, cap, || Ok((vec![], false))).unwrap_err();
    assert!(error.contains("not identified"), "{error}");
}

#[test]
#[cfg(target_os = "linux")]
fn push_reclaim_counts_allocated_bytes_like_the_push_preflight() {
    let parent = tempfile::tempdir().unwrap();
    let checkout = parent.path().join("checkout");
    let target = parent.path().join("target");
    fs::create_dir(&checkout).unwrap();
    fs::create_dir_all(target.join("debug")).unwrap();
    fs::write(
        target.join("CACHEDIR.TAG"),
        b"Signature: 8a477f597d28d172789f06886806bc55",
    )
    .unwrap();
    // 43 apparent bytes under a 1024-byte cap, but the file occupies a whole block on disk.
    assert!(crate::disk_hygiene::scan(&target).unwrap().0 < 1024);
    assert!(crate::disk_measure::allocated_bytes(&target).unwrap() > 1024);
    let error =
        reclaim_before_push_using(&checkout, &target, 1024, || Ok((vec![], false))).unwrap_err();
    assert!(
        error.starts_with("cargo_target_not_reclaimed:") && error.contains("retry the push"),
        "{error}"
    );
}
