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
fn refused_operations_leave_fresh_directory_empty() {
    let d = tempfile::tempdir().unwrap();
    assert!(matches!(
        acquire(d.path(), "missing", 0, &State::Free),
        Err(StoreError::Unknown(_))
    ));
    assert_eq!(fs::read_dir(d.path()).unwrap().count(), 0);
    assert!(create(d.path(), Path::new("slot"), "", "run", 0).is_err());
    assert_eq!(fs::read_dir(d.path()).unwrap().count(), 0);
}
