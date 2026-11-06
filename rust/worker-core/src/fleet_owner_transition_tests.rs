use super::*;
use crate::fleet_owner_lease::{bootstrap, migrate_v0, Read};

fn owner() -> Identity {
    Identity {
        pid: 7,
        start: "birth-a".into(),
        group: 7,
    }
}
fn busy() -> Record {
    let mut r = bootstrap("session-a", "run-a", 10).unwrap();
    r.identity = State::Busy(owner());
    r
}
fn tx(phase: Phase) -> Transaction {
    Transaction {
        id: "tx-a".into(),
        phase,
        successor: Successor::Id("next-a".into()),
    }
}
fn go(r: &Record, t: Transition) -> Result<Record, Refusal> {
    advance(r, r.epoch, &r.identity, t, 11)
}
fn reconcile(r: &Record) -> Transition {
    Transition::Reconcile {
        evidence_identity: r.identity.clone(),
        pending: State::Free,
        pause: State::Free,
        finalizer: State::Free,
        runners: State::Free,
    }
}
fn ordinary() -> Vec<Transition> {
    vec![
        Transition::Acquire(owner()),
        Transition::Release,
        Transition::Begin(tx(Phase::Prepared)),
        Transition::Launch("tx-a".into(), Successor::Id("next-a".into())),
        Transition::Confirm("tx-a".into()),
        Transition::Finish("tx-a".into()),
    ]
}

#[test]
fn comparison_clock_overflow_and_immutable_input() {
    let r = busy();
    let saved = r.clone();
    let next = go(&r, Transition::Release).unwrap();
    assert_eq!(r, saved);
    assert_eq!(
        (next.epoch, next.updated_at, next.identity.clone()),
        (1, 11, State::Free)
    );
    assert!(advance(&next, 0, &State::Free, Transition::Acquire(owner()), 11).is_err());
    assert!(advance(&r, 1, &r.identity, Transition::Release, 11).is_err());
    assert!(advance(&r, 0, &State::Free, Transition::Release, 11).is_err());
    let mut reused = owner();
    reused.start = "birth-b".into();
    assert!(advance(&r, 0, &State::Busy(reused), Transition::Release, 11).is_err());
    assert_eq!(
        advance(&r, 0, &r.identity, Transition::Release, 9),
        Err(Refusal::Clock)
    );
    let mut max = r.clone();
    max.epoch = u64::MAX;
    assert!(go(&max, Transition::Release).is_err());
    assert_eq!(max.epoch, u64::MAX);
    let acquired = go(&next, Transition::Acquire(owner())).unwrap();
    assert_eq!(acquired.identity, r.identity);
}

#[test]
fn transaction_state_matrix_and_round_trip() {
    for state in [
        State::Free,
        State::Busy(tx(Phase::Prepared)),
        State::Busy(tx(Phase::Launched)),
        State::Busy(tx(Phase::Confirmed)),
    ] {
        let mut r = busy();
        r.pending = state.clone();
        for (index, t) in ordinary().into_iter().enumerate() {
            let allowed = match (&state, index) {
                (State::Free, 1 | 2) => true,
                (State::Busy(t), 3) => t.phase == Phase::Prepared,
                (State::Busy(t), 4) => t.phase == Phase::Launched,
                (State::Busy(t), 5) => t.phase == Phase::Confirmed,
                _ => false,
            };
            assert_eq!(go(&r, t).is_ok(), allowed, "{state:?} / {index}");
        }
        for t in [
            Transition::Launch("wrong".into(), Successor::Unknown),
            Transition::Confirm("wrong".into()),
            Transition::Finish("wrong".into()),
            Transition::Begin(tx(Phase::Launched)),
            Transition::Begin(tx(Phase::Confirmed)),
        ] {
            assert!(go(&r, t).is_err());
        }
    }
    let free = bootstrap("session-a", "run-a", 10).unwrap();
    for t in ordinary().into_iter().skip(1) {
        assert!(go(&free, t).is_err());
    }
    let mut r = go(&busy(), Transition::Begin(tx(Phase::Prepared))).unwrap();
    for t in [
        Transition::Launch("tx-a".into(), Successor::Id("next-a".into())),
        Transition::Confirm("tx-a".into()),
    ] {
        r = go(&r, t).unwrap();
        r = serde_json::from_slice(&serde_json::to_vec(&r).unwrap()).unwrap();
        assert!(
            matches!(&r.pending, State::Busy(t) if t.successor == Successor::Id("next-a".into()))
        );
    }
    assert_eq!(
        go(&r, Transition::Finish("tx-a".into())).unwrap().pending,
        State::Free
    );
    r.pending = State::Busy(Transaction {
        successor: Successor::Unknown,
        ..tx(Phase::Launched)
    });
    assert!(go(&r, Transition::Confirm("tx-a".into())).is_err());
}

#[test]
fn blockers_unknown_expiry_and_reconciliation() {
    for field in 0..5 {
        for unknown in [false, true] {
            let mut r = busy();
            let lease = State::Busy(Lease {
                id: "lease-a".into(),
                owner: owner(),
                expires_at: 20,
            });
            match field {
                0 => {
                    r.pending = if unknown {
                        State::Unknown
                    } else {
                        State::Busy(tx(Phase::Prepared))
                    }
                }
                1 => r.pause = if unknown { State::Unknown } else { lease },
                2 => r.finalizer = if unknown { State::Unknown } else { lease },
                3 => {
                    r.runners = if unknown {
                        State::Unknown
                    } else if let State::Busy(l) = lease {
                        State::Busy(vec![l])
                    } else {
                        unreachable!()
                    }
                }
                _ => r.identity = State::Unknown,
            }
            assert!(go(&r, Transition::Release).is_err());
            assert!(go(&r, Transition::Acquire(owner())).is_err());
            if unknown || field == 4 {
                for t in ordinary() {
                    assert!(go(&r, t).is_err());
                }
                let mut evidence = reconcile(&r);
                if let Transition::Reconcile {
                    pending,
                    pause,
                    finalizer,
                    runners,
                    ..
                } = &mut evidence
                {
                    match field {
                        0 => *pending = State::Unknown,
                        1 => *pause = State::Unknown,
                        2 => *finalizer = State::Unknown,
                        3 => *runners = State::Unknown,
                        _ => {}
                    }
                }
                assert!(go(&busy(), evidence).is_err());
            }
            let mut free = r.clone();
            free.identity = State::Free;
            if field < 4 {
                assert!(go(&free, Transition::Acquire(owner())).is_err());
            }
            if (1..=3).contains(&field) && !unknown {
                for now in [20, 21] {
                    for t in ordinary() {
                        assert!(advance(&r, 0, &r.identity, t, now).is_err());
                    }
                    let next = advance(&r, 0, &r.identity, reconcile(&r), now).unwrap();
                    assert_eq!(next.identity, r.identity);
                    assert!(go(&next, Transition::Release).is_err()); // Clock, not permission.
                    assert!(advance(&next, 1, &next.identity, Transition::Release, now).is_ok());
                }
            }
        }
    }
}

#[test]
fn migration_and_exact_reconciliation_evidence() {
    let bytes = serde_json::to_vec(&serde_json::json!({"schema_version":0,
        "session_id":"session-a", "run_id":"run-a", "epoch":42,"identity":owner()}))
    .unwrap();
    let Read::Known(r) = migrate_v0("session-a", "run-a", Ok(bytes), 10) else {
        panic!()
    };
    for t in ordinary() {
        assert!(go(&r, t).is_err());
    }
    assert!(advance(&r, 41, &r.identity, reconcile(&r), 11).is_err());
    assert!(advance(&r, 42, &State::Free, reconcile(&r), 11).is_err());
    let mut wrong = reconcile(&r);
    if let Transition::Reconcile {
        evidence_identity, ..
    } = &mut wrong
    {
        *evidence_identity = State::Free;
    }
    assert!(go(&r, wrong).is_err());
    let next = go(&r, reconcile(&r)).unwrap();
    assert_eq!(next.identity, r.identity);
    assert!(!next.reconciliation_required);
    assert!(go(&next, Transition::Release).is_ok());
    let mut flagged = busy();
    flagged.reconciliation_required = true;
    for t in ordinary() {
        assert!(go(&flagged, t).is_err());
    }
}
