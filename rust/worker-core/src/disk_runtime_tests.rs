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
    let (cwds, building) = scan_processes(tmp.path()).unwrap();
    assert!(building);
    assert_eq!(cwds, vec![std::path::PathBuf::from("/protected/tree")]);
    fs::remove_file(pid.join("cwd")).unwrap();
    fs::write(pid.join("comm"), "node\n").unwrap();
    let (cwds, building) = scan_processes(tmp.path()).unwrap();
    assert!(!building);
    assert!(cwds.is_empty());
    fs::create_dir(pid.join("cwd")).unwrap();
    assert!(scan_processes(tmp.path()).is_err());
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
    let result = scan_processes(tmp.path());
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
