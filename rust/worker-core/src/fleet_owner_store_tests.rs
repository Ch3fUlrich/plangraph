use super::*;
use crate::fleet_owner_lease::{self as lease, Read};
use std::{
    fs,
    sync::{Arc, Barrier},
    time::{Duration, Instant},
};

fn owner() -> Identity {
    Identity {
        pid: 7,
        start: "first".into(),
        group: 7,
    }
}
fn acquire(
    root: &Path,
    slot: &str,
    epoch: u64,
    identity: &State<Identity>,
) -> Result<Record, StoreError> {
    cas(
        root,
        Path::new(slot),
        "session",
        "run",
        epoch,
        identity,
        Transition::Acquire(owner()),
        1,
    )
}
fn setup() -> tempfile::TempDir {
    let d = tempfile::tempdir().unwrap();
    create(d.path(), Path::new("slot"), "session", "run", 0).unwrap();
    d
}
fn bytes(root: &Path) -> Vec<u8> {
    fs::read(root.join("slot")).unwrap()
}

#[test]
fn contenders() {
    let d = setup();
    let barrier = Arc::new(Barrier::new(2));
    let threads: Vec<_> = (0..2)
        .map(|_| {
            let root = d.path().to_owned();
            let b = barrier.clone();
            std::thread::spawn(move || {
                b.wait();
                acquire(&root, "slot", 0, &State::Free)
            })
        })
        .collect();
    let results: Vec<_> = threads.into_iter().map(|t| t.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|r| matches!(r, Err(StoreError::StaleEpoch)))
            .count(),
        1
    );
    assert!(
        matches!(lease::decode("session", "run", Ok(bytes(d.path())), 1), Read::Known(r) if r.epoch == 1)
    );
}

#[test]
fn comparisons_preserve_bytes() {
    let d = setup();
    acquire(d.path(), "slot", 0, &State::Free).unwrap();
    let old = bytes(d.path());
    let mut reused = owner();
    reused.start = "second".into();
    for (epoch, identity) in [
        (0, State::Busy(owner())),
        (1, State::Free),
        (1, State::Busy(reused)),
    ] {
        assert!(cas(
            d.path(),
            Path::new("slot"),
            "session",
            "run",
            epoch,
            &identity,
            Transition::Release,
            1
        )
        .is_err());
        assert_eq!(bytes(d.path()), old);
    }
}

#[test]
fn crashes_and_retry() {
    for point in [1, 2] {
        let d = setup();
        let old = bytes(d.path());
        FAIL.with(|f| f.set(point));
        assert!(acquire(d.path(), "slot", 0, &State::Free).is_err());
        FAIL.with(|f| f.set(0));
        if point == 1 {
            assert_eq!(bytes(d.path()), old);
            assert!(fs::read_dir(d.path()).unwrap().any(|e| e
                .unwrap()
                .path()
                .extension()
                .is_some_and(|s| s == "tmp")));
            acquire(d.path(), "slot", 0, &State::Free).unwrap();
        } else {
            assert_ne!(bytes(d.path()), old);
        }
        assert!(matches!(
            acquire(d.path(), "slot", 0, &State::Free),
            Err(StoreError::StaleEpoch)
        ));
        assert!(
            matches!(lease::read(d.path(), Path::new("slot"), "session", "run", 1), Read::Known(r) if r.epoch == 1)
        );
    }
}

#[test]
fn unknown_is_local_and_never_rewritten() {
    let conflict = br#"{"schema_version":1,"session_id":"session","run_id":"run","epoch":0,"identity":"Free","updated_at":0,"reconciliation_required":false,"pending":"Unknown","pause":"Free","finalizer":"Free","runners":"Free"}"#;
    let v0 = br#"{"schema_version":0,"session_id":"session","run_id":"run","epoch":0,"identity":{"pid":7,"start":"first","group":7}}"#;
    for (data, reason) in [
        (b"broken".as_slice(), "malformed"),
        (b"{\"schema_version\":9}".as_slice(), "unsupported schema"),
        (conflict.as_slice(), "Unresolved"),
        (v0.as_slice(), "migration required"),
    ] {
        let d = setup();
        fs::write(d.path().join("slot"), data).unwrap();
        assert!(
            matches!(acquire(d.path(), "slot", 0, &State::Free), Err(StoreError::Unknown(r)) if r == reason)
        );
        assert_eq!(bytes(d.path()), data);
        create(d.path(), Path::new("other"), "session", "run", 0).unwrap();
        acquire(d.path(), "other", 0, &State::Free).unwrap();
    }
}

#[test]
fn bootstrap_missing_and_existing_paths() {
    let d = setup();
    assert!(create(d.path(), Path::new("slot"), "session", "run", 0).is_err());
    fs::remove_file(d.path().join("slot")).unwrap();
    assert!(
        matches!(acquire(d.path(), "slot", 0, &State::Free), Err(StoreError::Unknown(r)) if r == "io:NotFound")
    );
    assert!(!d.path().join("slot").exists());
    fs::create_dir(d.path().join("slot")).unwrap();
    assert!(create(d.path(), Path::new("slot"), "session", "run", 0).is_err());
    assert!(
        matches!(acquire(d.path(), "slot", 0, &State::Free), Err(StoreError::Unknown(r)) if r.starts_with("io:"))
    );
    create(d.path(), Path::new("other"), "session", "run", 0).unwrap();
    acquire(d.path(), "other", 0, &State::Free).unwrap();
}

#[test]
fn held_lock_is_bounded_and_local() {
    let d = setup();
    let _guard = crate::lockfile::try_lock(&d.path().join(".owner-locks/slot"))
        .unwrap()
        .unwrap();
    let start = Instant::now();
    assert!(matches!(
        acquire(d.path(), "slot", 0, &State::Free),
        Err(StoreError::Busy)
    ));
    assert!(start.elapsed() < Duration::from_secs(7));
    create(d.path(), Path::new("other"), "session", "run", 0).unwrap();
    acquire(d.path(), "other", 0, &State::Free).unwrap();
}

#[test]
fn symlinks_and_lexical_escape_refused() {
    let d = setup();
    fs::rename(d.path().join("slot"), d.path().join("target")).unwrap();
    let old = fs::read(d.path().join("target")).unwrap();
    std::os::unix::fs::symlink("target", d.path().join("slot")).unwrap();
    assert!(matches!(
        acquire(d.path(), "slot", 0, &State::Free),
        Err(StoreError::Unknown(_))
    ));
    assert!(create(d.path(), Path::new("slot"), "session", "run", 0).is_err());
    assert_eq!(fs::read(d.path().join("target")).unwrap(), old);
    assert!(acquire(d.path(), "../escape", 0, &State::Free).is_err());
}

#[test]
fn slow_winner_waits_then_observes_stale_epoch() {
    let d = setup();
    let guard = crate::lockfile::try_lock(&d.path().join(".owner-locks/slot"))
        .unwrap()
        .unwrap();
    std::thread::scope(|s| {
        let waiter = s.spawn(|| acquire(d.path(), "slot", 0, &State::Free));
        std::thread::sleep(Duration::from_millis(250));
        drop(guard);
        waiter.join().unwrap().unwrap();
    });
    assert!(matches!(
        acquire(d.path(), "slot", 0, &State::Free),
        Err(StoreError::StaleEpoch)
    ));
}

#[test]
fn lock_suffix_slots_are_independent() {
    let d = tempfile::tempdir().unwrap();
    for slot in ["x", "x.lock"] {
        create(d.path(), Path::new(slot), "session", "run", 0).unwrap();
    }
    let guard = crate::lockfile::try_lock(&d.path().join(".owner-locks/x"))
        .unwrap()
        .unwrap();
    acquire(d.path(), "x.lock", 0, &State::Free).unwrap();
    drop(guard);
    acquire(d.path(), "x", 0, &State::Free).unwrap();
    assert!(create(d.path(), Path::new(".owner-locks"), "session", "run", 0).is_err());
}

#[test]
fn refused_operations_leave_records_absent() {
    let d = tempfile::tempdir().unwrap();
    assert!(matches!(
        acquire(d.path(), "missing", 0, &State::Free),
        Err(StoreError::Unknown(_))
    ));
    assert!(!d.path().join("missing").exists());
    assert!(create(d.path(), Path::new("slot"), "", "run", 0).is_err());
    assert!(!d.path().join("slot").exists());
}

#[test]
fn temp_namespace_is_reserved() {
    let d = setup();
    FAIL.with(|f| f.set(1));
    assert!(acquire(d.path(), "slot", 0, &State::Free).is_err());
    FAIL.with(|f| f.set(0));
    let temp = fs::read_dir(d.path())
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .find(|n| n.to_string_lossy().starts_with(".owner-") && n != ".owner-locks")
        .unwrap();
    let slot = temp.to_str().unwrap();
    assert!(matches!(
        acquire(d.path(), slot, 1, &State::Busy(owner())),
        Err(StoreError::Refused(Refusal::Invalid))
    ));
    assert!(matches!(
        create(d.path(), Path::new(".owner-new.tmp"), "session", "run", 0),
        Err(StoreError::Refused(Refusal::Invalid))
    ));
}

#[test]
fn decisions_run_only_under_slot_lock() {
    let d = setup();
    store(d.path(), Path::new("slot"), Publish::Replace, |_, _| {
        assert!(
            crate::lockfile::try_lock(&d.path().join(".owner-locks/slot"))
                .unwrap()
                .is_none()
        );
        lease::bootstrap("session", "run", 0).map_err(StoreError::Refused)
    })
    .unwrap();
}

#[test]
fn degenerate_slots_do_not_escape_or_panic() {
    check_degenerate_slots(false);
}

#[test]
fn cas_degenerate_slots_do_not_escape_or_panic() {
    check_degenerate_slots(true);
}

fn check_degenerate_slots(replace: bool) {
    for slot in [
        "",
        ".",
        "..",
        "/",
        "a/..",
        "a/.",
        "nul\0name",
        "nul\0dir/slot",
    ] {
        let sandbox = tempfile::tempdir().unwrap();
        let root = sandbox.path().join("root");
        fs::create_dir(&root).unwrap();
        let result = if replace {
            if slot == "a/." {
                create(&root, Path::new(slot), "session", "run", 0).unwrap();
            }
            cas(
                &root,
                Path::new(slot),
                "session",
                "run",
                0,
                &State::Free,
                Transition::Acquire(owner()),
                0,
            )
        } else {
            create(&root, Path::new(slot), "session", "run", 0)
        };
        if slot == "a/." {
            assert!(result.is_ok());
        } else {
            assert!(matches!(result, Err(StoreError::Refused(Refusal::Invalid))));
        }
        assert_eq!(fs::read_dir(sandbox.path()).unwrap().count(), 1);
        if slot != "a/." {
            assert_eq!(fs::read_dir(&root).unwrap().count(), 0);
        }
    }
}

#[test]
fn create_loses_to_outside_writer_without_replacing_or_leaking() {
    let d = tempfile::tempdir().unwrap();
    BEFORE_PUBLISH.with(|hook| {
        *hook.borrow_mut() = Some(Box::new(|path| {
            assert!(
                crate::lockfile::try_lock(&path.parent().unwrap().join(".owner-locks/slot"))
                    .unwrap()
                    .is_none()
            );
            fs::write(path, b"outside writer").unwrap();
        }));
    });
    assert!(matches!(
        create(d.path(), Path::new("slot"), "session", "run", 0),
        Err(StoreError::Io(e)) if e.kind() == std::io::ErrorKind::AlreadyExists
    ));
    assert_eq!(bytes(d.path()), b"outside writer");
    assert!(fs::read_dir(d.path()).unwrap().all(|entry| {
        let name = entry.unwrap().file_name();
        name == "slot" || name == ".owner-locks"
    }));
}

#[test]
fn create_crash_after_publish_is_known_and_retry_exists() {
    let d = tempfile::tempdir().unwrap();
    FAIL.with(|f| f.set(2));
    let result = create(d.path(), Path::new("slot"), "session", "run", 0);
    FAIL.with(|f| f.set(0));
    assert!(result.is_err());
    assert!(matches!(
        lease::read(d.path(), Path::new("slot"), "session", "run", 0),
        Read::Known(r) if r.epoch == 0
    ));
    assert!(matches!(
        create(d.path(), Path::new("slot"), "session", "run", 0),
        Err(StoreError::Io(e)) if e.kind() == std::io::ErrorKind::AlreadyExists
    ));
}

#[test]
fn cas_replaces_created_record_and_advances_once() {
    let d = setup();
    let old = bytes(d.path());
    let record = acquire(d.path(), "slot", 0, &State::Free).unwrap();
    assert_eq!(record.epoch, 1);
    assert_ne!(bytes(d.path()), old);
    assert!(matches!(
        lease::read(d.path(), Path::new("slot"), "session", "run", 1),
        Read::Known(r) if r == record
    ));
}
