use super::*;
#[cfg(windows)]
#[test]
fn windows_slots_reject_junction_and_nonrelative_paths() {
    let d = tempfile::tempdir().unwrap();
    let root = d.path().join("root");
    let outside = d.path().join("outside");
    std::fs::create_dir(&root).unwrap();
    std::fs::create_dir(&outside).unwrap();
    std::fs::write(outside.join("lease"), b"outside").unwrap();
    std::fs::write(root.join("lease"), b"inside").unwrap();
    assert_eq!(confined_read(&root, Path::new("lease")).unwrap(), b"inside");
    for slot in [Path::new("../outside/lease"), outside.as_path()] {
        assert_eq!(
            confined_read(&root, slot).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }
    let link = root.join("junction");
    // Junction creation needs neither Developer Mode nor symlink privileges.
    let output = std::process::Command::new("cmd")
        .args(["/d", "/c", "mklink", "/J"])
        .arg(&link)
        .arg(&outside)
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        confined_read(&root, Path::new("junction/lease"))
            .unwrap_err()
            .kind(),
        io::ErrorKind::PermissionDenied
    );
    std::fs::remove_dir(link).unwrap();
}
fn owner() -> Identity {
    Identity {
        pid: 7,
        start: "birth-a".into(),
        group: 7,
    }
}
fn encoded(r: &Record) -> io::Result<Vec<u8>> {
    Ok(serde_json::to_vec(r).unwrap())
}
fn unknown(r: Read, reason: &str) {
    assert!(
        matches!(r, Read::Unknown { session_id, reason: why, alarm: true }
        if session_id == "session-a" && why.contains(reason))
    );
}
#[cfg(unix)]
#[test]
fn nul_slot_is_invalid_input() {
    let d = tempfile::tempdir().unwrap();
    assert_eq!(
        confined_read(d.path(), Path::new("bad\0slot"))
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidInput
    );
}
#[cfg(unix)]
#[test]
fn held_root_survives_symlink_swap() {
    let d = tempfile::tempdir().unwrap();
    let root = d.path().join("dir");
    let moved = d.path().join("moved");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(root.join("real.json"), b"held").unwrap();
    let held = std::fs::File::open(&root).unwrap();
    std::fs::rename(&root, &moved).unwrap();
    std::fs::write(d.path().join("real.json"), b"escaped").unwrap();
    std::os::unix::fs::symlink(d.path(), &root).unwrap();
    assert_eq!(
        confined_read_at(held, Path::new("real.json")).unwrap(),
        b"held"
    );
    assert!(confined_read(&root, Path::new("real.json")).is_err());
}
#[test]
fn json_paths_and_explicit_states() {
    let mut r = bootstrap("session-a", "run-a", 10).unwrap();
    assert_eq!(r.identity, State::Free);
    r.identity = State::Busy(owner());
    r.pending = State::Busy(Transaction {
        id: "tx-a".into(),
        phase: Phase::Launched,
        successor: Successor::Id("next-a".into()),
    });
    let l = Lease {
        id: "lease-a".into(),
        owner: owner(),
        expires_at: 20,
    };
    r.pause = State::Busy(l.clone());
    r.finalizer = State::Busy(l.clone());
    r.runners = State::Busy(vec![l]);
    assert_eq!(
        decode("session-a", "run-a", encoded(&r), 10),
        Read::Known(r.clone())
    );
    r.pending = State::Unknown;
    assert_eq!(
        decode("session-a", "run-a", encoded(&r), 10),
        Read::Known(r.clone())
    );
    let d = tempfile::tempdir().unwrap();
    let slot = Path::new("slot.json");
    assert_eq!(slot_path(d.path(), slot).unwrap(), d.path().join(slot));
    std::fs::write(d.path().join(slot), encoded(&r).unwrap()).unwrap();
    assert_eq!(
        read(d.path(), slot, "session-a", "run-a", 10),
        Read::Known(r)
    );
    for bad in ["", ".", "..", "../escape", "/absolute"] {
        assert!(slot_path(d.path(), Path::new(bad)).is_err());
    }
    for bad in ["", ".", "..", "bad/id", "bad\\id", "bad\n"] {
        assert!(bootstrap(bad, "run-a", 10).is_err());
        assert!(bootstrap("session-a", bad, 10).is_err());
    }
}
#[test]
fn loss_conflict_and_corruption_are_session_scoped() {
    let d = tempfile::tempdir().unwrap();
    let r = bootstrap("session-b", "run-b", 10).unwrap();
    std::fs::write(d.path().join("other"), encoded(&r).unwrap()).unwrap();
    unknown(
        read(d.path(), Path::new("lost"), "session-a", "run-a", 10),
        "NotFound",
    );
    unknown(
        decode(
            "session-a",
            "run-a",
            Err(io::ErrorKind::PermissionDenied.into()),
            10,
        ),
        "PermissionDenied",
    );
    unknown(
        read(d.path(), Path::new("."), "session-a", "run-a", 10),
        "InvalidInput",
    );
    unknown(
        decode("session-a", "run-a", Ok(b"broken".to_vec()), 10),
        "malformed",
    );
    let mut conflict = bootstrap("session-a", "run-a", 10).unwrap();
    conflict.pending = State::Busy(Transaction {
        id: "tx".into(),
        phase: Phase::Prepared,
        successor: Successor::Unknown,
    });
    unknown(
        decode("session-a", "run-a", encoded(&conflict), 10),
        "Unresolved",
    );
    let mut v = serde_json::to_value(&r).unwrap();
    v.as_object_mut().unwrap().remove("pause");
    unknown(
        decode(
            "session-a",
            "run-a",
            Ok(serde_json::to_vec(&v).unwrap()),
            10,
        ),
        "schema",
    );
    assert_eq!(
        read(d.path(), Path::new("other"), "session-b", "run-b", 10),
        Read::Known(r)
    );
}
#[test]
fn identity_reading_never_infers_release() {
    let mut r = bootstrap("session-a", "run-a", 10).unwrap();
    r.identity = State::Busy(owner());
    let mut reused = owner();
    reused.start = "birth-b".into();
    assert_ne!(r.identity, State::Busy(reused));
    assert_eq!(
        decode("session-a", "run-a", encoded(&r), 30),
        Read::Known(r.clone())
    );
    unknown(decode("session-a", "run-a", encoded(&r), 9), "Clock");
    r.identity = State::Unknown;
    assert_eq!(
        decode("session-a", "run-a", encoded(&r), 30),
        Read::Known(r.clone())
    );
    r.identity = State::Busy(Identity {
        start: String::new(),
        ..owner()
    });
    unknown(decode("session-a", "run-a", encoded(&r), 30), "Invalid");
}

#[test]
fn expiry_is_known_data() {
    for field in 0..3 {
        let mut r = bootstrap("session-a", "run-a", 10).unwrap();
        r.epoch = 42;
        r.identity = State::Busy(owner());
        let l = Lease {
            id: "lease-a".into(),
            owner: owner(),
            expires_at: 20,
        };
        match field {
            0 => r.pause = State::Busy(l),
            1 => r.finalizer = State::Busy(l),
            _ => r.runners = State::Busy(vec![l]),
        }
        assert!(r.expired_leases(19).is_empty());
        for now in [19, 20, 21] {
            assert_eq!(
                decode("session-a", "run-a", encoded(&r), now),
                Read::Known(r.clone())
            );
            if now >= 20 {
                let expired = r.expired_leases(now);
                assert_eq!(expired.len(), 1);
                assert_eq!(expired[0].id, "lease-a");
            }
        }
    }
}
#[test]
fn reconciliation_is_known_data() {
    let mut r = bootstrap("session-a", "run-a", 10).unwrap();
    r.reconciliation_required = true;
    assert_eq!(
        decode("session-a", "run-a", encoded(&r), 10),
        Read::Known(r)
    );
}
#[test]
fn migration_preserves_evidence_and_failures() {
    let mut v = serde_json::json!({"schema_version":0,"session_id":"session-a",
        "run_id":"run-a","epoch":42,"identity":owner()});
    let bytes = || Ok(serde_json::to_vec(&v).unwrap());
    unknown(
        decode("session-a", "run-a", bytes(), 10),
        "migration required",
    );
    let Read::Known(r) = migrate_v0("session-a", "run-a", bytes(), 10) else {
        panic!("migration failed")
    };
    assert_eq!((r.epoch, r.identity), (42, State::Busy(owner())));
    assert_eq!(
        (r.pending, r.pause, r.finalizer, r.runners),
        (
            State::Unknown,
            State::Unknown,
            State::Unknown,
            State::Unknown
        )
    );
    assert!(r.reconciliation_required);
    unknown(
        migrate_v0(
            "session-a",
            "run-a",
            Err(io::ErrorKind::PermissionDenied.into()),
            10,
        ),
        "PermissionDenied",
    );
    v["identity"]["start"] = "".into();
    unknown(
        migrate_v0(
            "session-a",
            "run-a",
            Ok(serde_json::to_vec(&v).unwrap()),
            10,
        ),
        "Invalid",
    );
    v["schema_version"] = 2.into();
    unknown(
        decode(
            "session-a",
            "run-a",
            Ok(serde_json::to_vec(&v).unwrap()),
            10,
        ),
        "unsupported schema",
    );
}

#[test]
fn required_fields_and_slot_identity() {
    let r = bootstrap("session-a", "run-a", 10).unwrap();
    unknown(
        decode("session-a", "other-run", encoded(&r), 10),
        "slot mismatch",
    );
    let value = serde_json::to_value(&r).unwrap();
    for field in value.as_object().unwrap().keys() {
        let mut missing = value.clone();
        missing.as_object_mut().unwrap().remove(field);
        assert!(matches!(
            decode(
                "session-a",
                "run-a",
                Ok(serde_json::to_vec(&missing).unwrap()),
                10
            ),
            Read::Unknown { alarm: true, .. }
        ));
    }
}
