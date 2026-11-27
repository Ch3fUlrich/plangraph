use super::*;
#[cfg(target_os = "linux")]
use crate::reap_journal::with_inventory;
use std::time::Duration;

#[cfg(target_os = "linux")]
const CAP: u64 = 1000;

/// `<root>/cache/cargo-target` and its review pool.
fn primary(root: &Path) -> PathBuf {
    root.join("cache/cargo-target")
}

fn set_modified(path: &Path, modified: SystemTime) {
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_WRITE_ATTRIBUTES,
        };

        // Both the directory and its files contribute to the age of a candidate.
        options
            .access_mode(FILE_WRITE_ATTRIBUTES)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS);
    }
    options.open(path).unwrap().set_modified(modified).unwrap();
}

fn age(path: &Path, seconds: u64) {
    set_modified(path, SystemTime::now() - Duration::from_secs(seconds));
}

/// A build unit of `crate_name` under `<slot>/debug/build`: `bytes` in its marker, `seconds` old.
fn unit(slot: &Path, name: &str, bytes: usize, seconds: u64) -> PathBuf {
    let dir = slot.join("debug/build").join(name);
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("output"), vec![0u8; bytes]).unwrap();
    age(&dir.join("output"), seconds);
    age(&dir, seconds);
    dir
}

/// A review slot with an evictable old unit and a newer one that stays: 800 bytes.
#[cfg(unix)]
fn review_slot(primary: &Path, id: &str) -> (PathBuf, PathBuf) {
    let slot = crate::cargo_pool::pool_root(primary)
        .unwrap()
        .join(format!("slot-review-{id}"));
    let old = unit(&slot, "foo-1111111111111111", 400, 90_000);
    unit(&slot, "foo-2222222222222222", 400, 80_000);
    (slot.canonicalize().unwrap(), old)
}

#[cfg(target_os = "linux")]
fn mounted(path: &Path) -> Vec<PathBuf> {
    vec![path.canonicalize().unwrap()]
}

#[cfg(target_os = "linux")]
#[test]
fn aggregate_trim_preserves_nested_mount_in_stale_build_unit() {
    for apply in [true, false] {
        let tmp = tempfile::tempdir().unwrap();
        let primary = primary(tmp.path());
        let slots: Vec<_> = ["a", "b", "c", "d"]
            .map(|id| review_slot(&primary, id))
            .into();
        // Every slot is below its own cap (800 < 1000); the four together (3200) are above the
        // fixed budget of three caps. The mount sits in the unit of the first slot the pass trims.
        let mount = slots[0].1.join("out/mnt");
        fs::create_dir_all(&mount).unwrap();
        fs::write(mount.join("data"), [0u8; 10]).unwrap();
        for old in [
            mount.join("data"),
            mount.clone(),
            mount.parent().unwrap().into(),
            slots[0].1.clone(),
        ] {
            age(&old, 90_000);
        }
        let paths: Vec<_> = slots.iter().map(|(slot, _)| slot.clone()).collect();
        let mut report = Report::default();
        let total = with_inventory(Some(mounted(&mount)), || {
            enforce_review_slots(&primary, &paths, CAP, apply, &mut report).unwrap()
        });
        assert!(mount.join("data").exists(), "mounted contents kept");
        assert!(slots[0].1.exists());
        assert!(
            report
                .skipped
                .iter()
                .any(|line| line.contains(slots[0].1.to_str().unwrap())),
            "{:?}",
            report.skipped
        );
        // The pass moved on to the next slot and reclaimed its eligible unit.
        assert_eq!(slots[1].1.exists(), !apply);
        assert_eq!(total, 3210 - 400, "retained bytes stay in the total");
        assert_eq!(report.freed_bytes, if apply { 400 } else { 0 });
        assert!(!report.projected.covers(&slots[0].1));
        assert_eq!(report.projected.covers(&slots[1].1), !apply);
    }
}

#[cfg(target_os = "linux")]
#[test]
fn trim_preserves_build_unit_beneath_ancestor_bind_mount() {
    for apply in [true, false] {
        let tmp = tempfile::tempdir().unwrap();
        let primary = primary(tmp.path());
        let (slot, old) = review_slot(&primary, "a");
        // A bind mount of `debug` (same device, so metadata cannot tell) above the stale unit.
        let mut report = Report::default();
        let total = with_inventory(Some(mounted(&slot.join("debug"))), || {
            enforce_review_slots(
                &primary,
                std::slice::from_ref(&slot),
                500,
                apply,
                &mut report,
            )
            .unwrap()
        });
        assert!(old.join("output").exists());
        assert!(report.skipped.iter().any(|l| l.contains("mount above")));
        assert_eq!(report.freed_bytes, 0);
        assert!(report.actions.is_empty(), "{:?}", report.actions);
        assert!(!report.projected.covers(&old));
        assert_eq!(total, 800);
    }
}

#[cfg(target_os = "linux")]
#[test]
fn pool_fallback_preserves_mount_root_and_contents() {
    for apply in [true, false] {
        let tmp = tempfile::tempdir().unwrap();
        let primary = primary(tmp.path());
        // No stale unit: only the whole-profile fallback can reach these directories.
        fs::create_dir_all(primary.join("debug")).unwrap();
        fs::write(primary.join("debug/data"), [0u8; 800]).unwrap();
        age(&primary.join("debug/data"), 5000);
        age(&primary.join("debug"), 5000);
        fs::create_dir_all(primary.join("release")).unwrap();
        fs::write(primary.join("release/data"), [0u8; 1000]).unwrap();
        let root = primary.canonicalize().unwrap().join("debug");
        let mut report = Report::default();
        let total = with_inventory(Some(vec![root.clone()]), || {
            enforce_pool_cap(
                &primary,
                std::slice::from_ref(&primary),
                500,
                apply,
                &mut report,
            )
            .unwrap()
        });
        assert!(root.join("data").exists(), "mount root and contents kept");
        assert!(report.skipped.iter().any(|l| l.contains("mount inside")));
        assert!(report.actions.iter().all(|a| !a.contains("debug")));
        assert!(!report.projected.covers(&root));
        assert_eq!(primary.join("release").exists(), !apply);
        assert_eq!(total, 800, "the refused bytes stay in the total");
    }
}

#[cfg(target_os = "linux")]
#[test]
fn unreadable_inventory_leaves_the_bytes_and_records_a_skip() {
    for apply in [true, false] {
        let tmp = tempfile::tempdir().unwrap();
        let primary = primary(tmp.path());
        let (slot, old) = review_slot(&primary, "a");
        let mut report = Report::default();
        with_inventory(None, || {
            enforce_review_slots(
                &primary,
                std::slice::from_ref(&slot),
                500,
                apply,
                &mut report,
            )
            .unwrap()
        });
        assert!(old.exists());
        assert!(report.skipped.iter().any(|l| l.contains("unreadable")));
        assert_eq!(report.freed_bytes, 0);
    }
}

#[test]
fn dry_run_counts_a_subtree_once_across_unit_and_profile_passes() {
    let expected = |apply| {
        let tmp = tempfile::tempdir().unwrap();
        let primary = primary(tmp.path());
        unit(&primary, "foo-1111111111111111", 400, 90_000);
        unit(&primary, "foo-2222222222222222", 400, 80_000);
        fs::write(primary.join("debug/extra"), [0u8; 200]).unwrap();
        age(&primary.join("debug/extra"), 5000);
        age(&primary.join("debug"), 5000);
        fs::create_dir_all(primary.join("release")).unwrap();
        fs::write(primary.join("release/data"), [0u8; 300]).unwrap();
        // The newest by far, in either mode: the profile pass reaches `debug` first.
        for newest in [primary.join("release/data"), primary.join("release")] {
            set_modified(&newest, SystemTime::now() + Duration::from_secs(1000));
        }
        let mut report = Report::default();
        // Unit pass removes 400 of 1300; the profile fallback removes the rest of `debug` (600):
        // `release` (300) is what remains, and the loop stops there.
        // Run the shared trim with an isolated fixture. The pool's lease requires Unix.
        trim_slot_to(&primary.canonicalize().unwrap(), 300, apply, &mut report).unwrap()
    };
    assert_eq!(expected(true), 300);
    assert_eq!(expected(false), 300);
}

#[cfg(unix)]
#[test]
fn slots_outside_the_trusted_pool_are_not_trimmed() {
    let tmp = tempfile::tempdir().unwrap();
    let primary = primary(tmp.path());
    let (slot, old) = review_slot(&primary, "a");
    let outside = tmp.path().join("elsewhere");
    let (stranger, stale) = review_slot(&outside.join("cargo-target"), "b");
    let linked = slot.with_file_name("slot-review-link");
    std::os::unix::fs::symlink(&stranger, &linked).unwrap();
    let mut report = Report::default();
    enforce_review_slots(&primary, &[stranger, linked], 100, true, &mut report).unwrap();
    assert!(stale.exists() && old.exists());
    assert_eq!(
        report
            .skipped
            .iter()
            .filter(|l| l.contains("untrusted slot"))
            .count(),
        2,
        "{:?}",
        report.skipped
    );
}

#[cfg(unix)]
#[test]
fn an_unmeasurable_slot_is_reported_incomplete() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } == 0 {
        return; // root reads everything
    }
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let primary = primary(tmp.path());
    let (slot, old) = review_slot(&primary, "a");
    let hidden = slot.join("hidden");
    fs::create_dir(&hidden).unwrap();
    let original_permissions = fs::metadata(&hidden).unwrap().permissions();
    fs::set_permissions(&hidden, fs::Permissions::from_mode(0o000)).unwrap();
    let mut report = Report::default();
    let result = enforce_review_slots(&primary, &[slot], 100, true, &mut report);
    fs::set_permissions(&hidden, original_permissions).unwrap();
    result.unwrap();
    assert!(old.exists());
    assert!(report.alarm);
    assert!(report
        .skipped
        .iter()
        .any(|l| l.contains("total incomplete")));
}

#[cfg(target_os = "linux")]
#[test]
fn pressure_keeps_leases_mounts_source_and_late_references() {
    use crate::{
        disk_hygiene::{
            reclaim, Budget, Candidate, Device, HygieneInventory, HygieneReport, ReclaimChecks,
            Thresholds,
        },
        reap_journal::with_inventory,
    };
    use std::{
        cell::Cell,
        os::unix::fs::MetadataExt,
        time::{Duration, Instant, SystemTime},
    };

    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let cutoff = SystemTime::now();
    let old = cutoff - Duration::from_secs(3600);
    let mut candidates = Vec::new();
    for name in [
        "idle", "leased", "mount", "source", "young", "late", "pinned",
    ] {
        let path = root.join(name);
        fs::create_dir_all(path.join("nested")).unwrap();
        let marker = if name == "source" { ".git" } else { "cache" };
        let file = path.join("nested").join(marker);
        fs::write(&file, [0u8; 8192]).unwrap();
        for entry in [&path, &path.join("nested"), &file] {
            fs::File::open(entry).unwrap().set_modified(old).unwrap();
        }
        if name == "young" {
            fs::File::open(file).unwrap().set_modified(cutoff).unwrap();
        }
        candidates.push(Candidate::new(path, root.clone(), cutoff).unwrap());
    }
    let lease = try_exclusive_lease(&root.join("leased")).unwrap();
    assert!(lease.is_some());
    let calls = Cell::new(0);
    let inventory = |_| {
        calls.set(calls.get() + 1);
        let mut rows = HygieneInventory::default();
        if calls.get() > 1 {
            let fd = root.join("late/nested/cache");
            rows.open_paths
                .insert(fd.into_os_string().into_encoded_bytes());
        }
        Ok(rows)
    };
    let id = root.metadata().unwrap().dev();
    let mut report = HygieneReport::new(Duration::ZERO, false);
    let device = Device::new(id, root.clone(), Some(0), Thresholds::default());
    report.devices.insert(id, device);
    let checks = ReclaimChecks {
        inventory: &inventory,
        space: &|_| Ok(0),
        protection: &|item, _| Ok(item.path.ends_with("pinned").then_some("pinned")),
    };
    with_inventory(Some(vec![root.join("mount/nested")]), || {
        assert!(reclaim(
            &mut report,
            candidates,
            &Budget::new(Instant::now(), &Instant::now),
            &checks
        ));
    });
    assert!(!root.join("idle").exists());
    assert_eq!(report.kept.len(), 6);
    for (name, reason) in [
        ("leased", "leased"),
        ("mount", "unsafe"),
        ("source", "unsafe"),
        ("young", "young"),
        ("late", "busy"),
        ("pinned", "pinned"),
    ] {
        let path = root.join(name);
        assert!(path.is_dir());
        assert!(report
            .kept
            .iter()
            .any(|row| row.path == path && row.reason == reason));
    }
}
