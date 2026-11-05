    use super::*;
    fn advance(r: &Record, e: u64, i: &State<Identity>, t: Transition) -> Result<Record, Refusal> { super::advance(r, e, i, t, r.updated_at) }
    fn owner() -> Identity { Identity { pid: 7, start: "birth-a".into(), group: 7, } }
    fn encoded(r: &Record) -> io::Result<Vec<u8>> { Ok(serde_json::to_vec(r).unwrap()) }
    fn unknown(r: Read, reason: &str) { assert!(
            matches!(r, Read::Unknown { session_id, reason: why, alarm: true }
            if session_id == "session-a" && why.contains(reason)) ); }
    #[test]
    fn reconciliation_owner_is_documented() { assert!(include_str!("fleet_owner_lease.rs").lines().take(12).any(|l| l.contains("Part 2 owns explicit expected-epoch reconciliation"))); }
    #[test]
    fn phase_evidence_and_clock() {
        let mut r = bootstrap("session-a", "run-a", 10).unwrap(); r.identity = State::Busy(owner());
        r.pending = State::Busy(Transaction { id: "tx-a".into(), phase: Phase::Prepared, successor: Successor::Unknown });
        assert!(advance(&r, 0, &r.identity, Transition::Complete("tx-a".into())).is_err());
        let launched = super::advance(&r, 0, &r.identity, Transition::Launch("tx-a".into(), "next-a".into()), 11).unwrap();
        assert_eq!(launched.updated_at, 11);
        let done = advance(&launched, 1, &r.identity, Transition::Complete("tx-a".into())).unwrap();
        assert!(matches!(done.pending, State::Busy(Transaction { phase: Phase::Confirmed, successor: Successor::Id(ref id), .. }) if id == "next-a"));
        assert!(advance(&done, 2, &r.identity, Transition::Complete("tx-a".into())).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn held_root_survives_symlink_swap() {
        let d = tempfile::tempdir().unwrap(); let root = d.path().join("dir"); let moved = d.path().join("moved");
        std::fs::create_dir(&root).unwrap(); std::fs::write(root.join("real.json"), b"held").unwrap();
        let held = std::fs::File::open(&root).unwrap(); std::fs::rename(&root, &moved).unwrap();
        std::fs::write(d.path().join("real.json"), b"escaped").unwrap(); std::os::unix::fs::symlink(d.path(), &root).unwrap();
        assert_eq!(confined_read_at(held, Path::new("real.json")).unwrap(), b"held");
        assert!(confined_read(&root, Path::new("real.json")).is_err());
    }
    #[test]
    fn advance_guards_and_exact_epoch() {
        let r = bootstrap("session-a", "run-a", 10).unwrap();
        assert!(advance(&r, 1, &State::Free, Transition::Acquire(owner())).is_err());
        let busy = advance(&r, 0, &State::Free, Transition::Acquire(owner())).unwrap();
        assert_eq!(busy.epoch, 1);
        assert!(advance(&busy, 0, &State::Free, Transition::Acquire(owner())).is_err());
        let mut reused = owner(); reused.start = "birth-b".into();
        assert!(advance(&busy, 1, &State::Busy(reused), Transition::Release).is_err());
        assert_eq!(advance(&busy, 1, &busy.identity, Transition::Release).unwrap().epoch, 2);
        let mut pending = busy.clone(); pending.pending = State::Busy(Transaction {
            id: "tx-a".into(), phase: Phase::Prepared, successor: Successor::Unknown });
        assert!(advance(&pending, 1, &pending.identity, Transition::Complete("tx-b".into())).is_err());
        assert!(advance(&pending, 1, &pending.identity, Transition::Complete("tx-a".into())).is_err());
        for now in [19, 20, 21] { for field in 0..4 {
            let mut blocked = busy.clone(); let l = Lease { id: "lease-a".into(), owner: owner(), expires_at: 20 };
            match field { 0 => blocked.pending = pending.pending.clone(), 1 => blocked.finalizer = State::Busy(l),
                2 => blocked.pause = State::Busy(l), _ => blocked.runners = State::Busy(vec![l]) }
            assert_eq!(decode("session-a", "run-a", encoded(&blocked), now), Read::Known(blocked.clone()));
            assert!(advance(&blocked, 1, &blocked.identity, Transition::Release).is_err());
        } }
    }
    #[test]
    fn json_paths_and_explicit_states() { let mut r = bootstrap("session-a", "run-a", 10).unwrap();
        assert_eq!(r.identity, State::Free); r.identity = State::Busy(owner());
        r.pending = State::Busy(Transaction { id: "tx-a".into(), phase: Phase::Launched,
            successor: Successor::Id("next-a".into()), }); let l = Lease { id: "lease-a".into(),
            owner: owner(), expires_at: 20, }; r.pause = State::Busy(l.clone());
        r.finalizer = State::Busy(l.clone()); r.runners = State::Busy(vec![l]); assert_eq!(
            decode("session-a", "run-a", encoded(&r), 10), Read::Known(r.clone()) );
        r.pending = State::Unknown; assert_eq!( decode("session-a", "run-a", encoded(&r), 10),
            Read::Known(r.clone()) ); let d = tempfile::tempdir().unwrap();
        let slot = Path::new("slot.json");
        assert_eq!(slot_path(d.path(), slot).unwrap(), d.path().join(slot));
        std::fs::write(d.path().join(slot), encoded(&r).unwrap()).unwrap(); assert_eq!(
            read(d.path(), slot, "session-a", "run-a", 10), Read::Known(r) );
        for bad in ["", ".", "..", "../escape", "/absolute"] {
            assert!(slot_path(d.path(), Path::new(bad)).is_err()); }
        for bad in ["", ".", "..", "bad/id", "bad\\id", "bad\n"] {
            assert!(bootstrap(bad, "run-a", 10).is_err());
            assert!(bootstrap("session-a", bad, 10).is_err()); } }
    #[test]
    fn loss_conflict_and_corruption_are_session_scoped() { let d = tempfile::tempdir().unwrap();
        let r = bootstrap("session-b", "run-b", 10).unwrap();
        std::fs::write(d.path().join("other"), encoded(&r).unwrap()).unwrap(); unknown(
            read(d.path(), Path::new("lost"), "session-a", "run-a", 10), "NotFound", ); unknown( decode(
                "session-a", "run-a", Err(io::ErrorKind::PermissionDenied.into()), 10, ),
            "PermissionDenied", ); unknown( read(d.path(), Path::new("."), "session-a", "run-a", 10),
            "InvalidInput", ); unknown( decode("session-a", "run-a", Ok(b"broken".to_vec()), 10),
            "malformed", ); let mut conflict = bootstrap("session-a", "run-a", 10).unwrap();
        conflict.pending = State::Busy(Transaction { id: "tx".into(), phase: Phase::Prepared,
            successor: Successor::Unknown, }); unknown(
            decode("session-a", "run-a", encoded(&conflict), 10), "Unresolved", );
        let mut v = serde_json::to_value(&r).unwrap(); v.as_object_mut().unwrap().remove("pause");
        unknown( decode( "session-a", "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10, ), "schema", );
        assert_eq!( read(d.path(), Path::new("other"), "session-b", "run-b", 10), Read::Known(r) ); }
    #[test]
    fn identity_reading_never_infers_release() {
        let mut r = bootstrap("session-a", "run-a", 10).unwrap(); r.identity = State::Busy(owner());
        let mut reused = owner(); reused.start = "birth-b".into();
        assert_ne!(r.identity, State::Busy(reused)); assert_eq!(
            decode("session-a", "run-a", encoded(&r), 30), Read::Known(r.clone()) );
        unknown(decode("session-a", "run-a", encoded(&r), 9), "Clock"); r.identity = State::Unknown;
        assert_eq!( decode("session-a", "run-a", encoded(&r), 30), Read::Known(r.clone()) );
        r.identity = State::Busy(Identity { start: String::new(), ..owner() });
        unknown(decode("session-a", "run-a", encoded(&r), 30), "Invalid"); }
    #[test]
    fn migration_is_explicit_and_non_authorizing() {
        let mut v = serde_json::json!({"schema_version":0,"session_id":"session-a",
            "run_id":"run-a","epoch":42,"identity":owner()}); unknown(decode("session-a", "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10), "migration required");
        let r = migrate_v0( "session-a",
            "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10, ).unwrap();
        assert!(advance(&r, 42, &r.identity, Transition::Acquire(owner())).is_err());
        assert!(advance(&r, 42, &r.identity, Transition::Release).is_err());
        assert_eq!((r.epoch, r.identity.clone()), (42, State::Busy(owner()))); assert_eq!(
            (r.pending, r.pause, r.finalizer, r.runners), ( State::Unknown, State::Unknown,
                State::Unknown, State::Unknown ) ); v["identity"]["start"] = "".into(); assert!(migrate_v0("session-a", "run-a", Ok(serde_json::to_vec(&v).unwrap()), 10).is_err());
        v["schema_version"] = 2.into(); unknown( decode( "session-a", "run-a",
                Ok(serde_json::to_vec(&v).unwrap()), 10, ), "unsupported", ); } 
