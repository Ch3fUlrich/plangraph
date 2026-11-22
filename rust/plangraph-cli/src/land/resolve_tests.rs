use super::resolve::*;
use std::collections::BTreeMap;

const REPO: &str = "o/r";

fn ledger() -> Ledger {
    Ledger::new(BTreeMap::from([
        ("b1".to_owned(), "writer-1".to_owned()),
        ("b2".to_owned(), "writer-2".to_owned()),
    ]))
}

fn ap(principal: &str, key: &str, event: Event, now: i64) -> Append {
    Append {
        principal: principal.into(),
        repo: REPO.into(),
        key: key.into(),
        event,
        now,
    }
}

fn request(build: &str, position: u32, onto: &str) -> Event {
    Event::Request {
        build: build.into(),
        position,
        part: 7,
        head: "h7".into(),
        onto: onto.into(),
    }
}

fn open_request(l: &mut Ledger, now: i64) -> String {
    match l.append(&ap("builder", "k-req", request("b1", 1, "p1"), now)) {
        Ok(Outcome::Request { id, .. }) => id,
        other => panic!("{other:?}"),
    }
}

fn submit(
    l: &mut Ledger,
    key: &str,
    req: &str,
    replaces: Option<&str>,
    commit: &str,
    now: i64,
) -> Result<Outcome, Refusal> {
    l.append(&ap(
        "writer-1",
        key,
        Event::Submit {
            request: req.into(),
            replaces: replaces.map(Into::into),
            commit: commit.into(),
        },
        now,
    ))
}

fn sub_id(o: Result<Outcome, Refusal>) -> String {
    match o {
        Ok(Outcome::Submission { id }) => id,
        other => panic!("{other:?}"),
    }
}

fn registrar(l: &mut Ledger, key: &str, e: Event, now: i64) -> Result<Outcome, Refusal> {
    l.append(&ap("registrar", key, e, now))
}

fn builder(l: &mut Ledger, key: &str, e: Event, now: i64) -> Result<Outcome, Refusal> {
    l.append(&ap("builder", key, e, now))
}

#[test]
fn a_request_has_a_thirty_minute_deadline_and_a_repeat_returns_the_first_or_is_a_mismatch() {
    let mut l = ledger();
    let first = l
        .append(&ap("builder", "k1", request("b1", 1, "p1"), 100))
        .unwrap();
    assert_eq!(
        first,
        Outcome::Request {
            id: "r1".into(),
            deadline: 100 + DEADLINE_SECS
        }
    );
    // A lost answer: the retry (same key) and a fresh key with the same payload both return the original.
    assert_eq!(
        l.append(&ap("builder", "k1", request("b1", 1, "p1"), 900))
            .unwrap(),
        first
    );
    assert_eq!(
        l.append(&ap("builder", "k2", request("b1", 1, "p1"), 900))
            .unwrap(),
        first
    );
    // Another prefix at the same position: refused.
    assert_eq!(
        l.append(&ap("builder", "k3", request("b1", 1, "other"), 900)),
        Err(Refusal::RequestMismatch)
    );
}

#[test]
fn a_correction_naming_the_open_submission_supersedes_it_and_the_late_retry_of_the_first_changes_nothing(
) {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "key-a", &req, None, "ca", 10));
    // The answer for A was lost; the writer corrects with B naming A.
    let b = sub_id(submit(&mut l, "key-b", &req, Some(&a), "cb", 20));
    // The delayed retry of A carries A's key: the first result comes back, B stays open.
    assert_eq!(
        submit(&mut l, "key-a", &req, None, "ca", 30).unwrap(),
        Outcome::Submission { id: a.clone() }
    );
    // The registrar and the builder cannot touch the superseded A.
    assert!(matches!(
        registrar(
            &mut l,
            "s-a",
            Event::Secured {
                submission: a.clone()
            },
            40
        ),
        Err(Refusal::ExpectationFailed(_))
    ));
    registrar(
        &mut l,
        "s-b",
        Event::Secured {
            submission: b.clone(),
        },
        40,
    )
    .unwrap();
    assert!(builder(&mut l, "adopt-a", Event::Adopt { submission: a }, 50).is_err());
    builder(
        &mut l,
        "adopt-b",
        Event::Adopt {
            submission: b.clone(),
        },
        50,
    )
    .unwrap();
    assert_eq!(l.adopted("b1")[0].submission, b);
    assert_eq!(l.adopted("b1")[0].commit, "cb");
}

#[test]
fn a_correction_naming_a_submission_that_is_no_longer_open_is_refused_and_so_is_one_naming_nothing()
{
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &req, None, "ca", 10));
    let b = sub_id(submit(&mut l, "kb", &req, Some(&a), "cb", 20));
    assert!(
        matches!(
            submit(&mut l, "kc", &req, Some(&a), "cc", 30),
            Err(Refusal::ExpectationFailed(_))
        ),
        "A is not open any more"
    );
    assert!(
        matches!(
            submit(&mut l, "kd", &req, None, "cd", 30),
            Err(Refusal::ExpectationFailed(_))
        ),
        "a second submit that names nothing"
    );
    let c = sub_id(submit(&mut l, "ke", &req, Some(&b), "ce", 30));
    assert_ne!(c, b, "naming the open submission supersedes it");
}

#[test]
fn a_correction_is_timely_until_the_deadline_and_its_adoption_may_come_after_it() {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &req, None, "ca", 10));
    // Minute 29: a correction while the first is still being fetched.
    let b = sub_id(submit(&mut l, "kb", &req, Some(&a), "cb", 29 * 60));
    // Secured and adopted after the deadline: its submit was timely.
    registrar(
        &mut l,
        "sb",
        Event::Secured {
            submission: b.clone(),
        },
        DEADLINE_SECS + 120,
    )
    .unwrap();
    builder(
        &mut l,
        "ab",
        Event::Adopt { submission: b },
        DEADLINE_SECS + 130,
    )
    .unwrap();
    // A supersession attempt after the deadline is refused.
    let mut l2 = ledger();
    let req2 = open_request(&mut l2, 0);
    let a2 = sub_id(submit(&mut l2, "ka", &req2, None, "ca", 10));
    assert_eq!(
        submit(&mut l2, "kb", &req2, Some(&a2), "cb", DEADLINE_SECS + 1),
        Err(Refusal::PastDeadline)
    );
}

#[test]
fn only_the_recorded_writer_submits_and_each_verb_has_its_principal() {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let other = l.append(&ap(
        "writer-2",
        "k",
        Event::Submit {
            request: req.clone(),
            replaces: None,
            commit: "c".into(),
        },
        1,
    ));
    assert_eq!(other, Err(Refusal::NotAllowed));
    let a = sub_id(submit(&mut l, "ka", &req, None, "ca", 2));
    assert_eq!(
        builder(
            &mut l,
            "x",
            Event::Secured {
                submission: a.clone()
            },
            3
        ),
        Err(Refusal::NotAllowed)
    );
    assert_eq!(
        registrar(&mut l, "y", Event::Adopt { submission: a }, 3),
        Err(Refusal::NotAllowed)
    );
}

#[test]
fn a_second_submission_after_a_reject_is_accepted_and_adjudicated() {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &req, None, "ca", 10));
    registrar(
        &mut l,
        "sa",
        Event::Secured {
            submission: a.clone(),
        },
        11,
    )
    .unwrap();
    builder(
        &mut l,
        "ra",
        Event::Reject {
            submission: a,
            reason: "wrong parents".into(),
        },
        12,
    )
    .unwrap();
    let b = sub_id(submit(&mut l, "kb", &req, None, "cb", 20));
    registrar(
        &mut l,
        "sb",
        Event::Secured {
            submission: b.clone(),
        },
        21,
    )
    .unwrap();
    builder(&mut l, "ab", Event::Adopt { submission: b }, 22).unwrap();
    assert_eq!(l.adopted("b1").len(), 1);
}

#[test]
fn one_adoption_per_request_and_a_decision_needs_the_secured_open_submission() {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &req, None, "ca", 10));
    // Not secured yet: neither a reject nor an adoption is accepted.
    assert!(builder(
        &mut l,
        "r",
        Event::Reject {
            submission: a.clone(),
            reason: "x".into()
        },
        11
    )
    .is_err());
    assert!(builder(
        &mut l,
        "a",
        Event::Adopt {
            submission: a.clone()
        },
        11
    )
    .is_err());
    registrar(
        &mut l,
        "sa",
        Event::Secured {
            submission: a.clone(),
        },
        12,
    )
    .unwrap();
    builder(
        &mut l,
        "a2",
        Event::Adopt {
            submission: a.clone(),
        },
        13,
    )
    .unwrap();
    assert!(
        builder(&mut l, "a3", Event::Adopt { submission: a }, 14).is_err(),
        "decided once"
    );
}

#[test]
fn a_timeout_needs_the_deadline_passed_nothing_adopted_and_no_open_submission() {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &req, None, "ca", 10));
    let late = DEADLINE_SECS + 5;
    assert!(
        builder(
            &mut l,
            "t1",
            Event::TimedOut {
                request: req.clone()
            },
            late
        )
        .is_err(),
        "a submission is open"
    );
    registrar(&mut l, "sa", Event::NeverSecured { submission: a }, 20).unwrap();
    assert!(
        builder(
            &mut l,
            "t2",
            Event::TimedOut {
                request: req.clone()
            },
            100
        )
        .is_err(),
        "before the deadline"
    );
    builder(
        &mut l,
        "t3",
        Event::TimedOut {
            request: req.clone(),
        },
        late,
    )
    .unwrap();
    assert_eq!(l.timed_out("b1"), vec![(7, 1)]);
    assert!(
        submit(&mut l, "kb", &req, None, "cb", late + 1).is_err(),
        "nothing is submitted after a timeout"
    );
}

#[test]
fn a_carry_is_refused_while_a_submission_is_open_after_an_adoption_and_when_the_parents_differ() {
    let mut l = ledger();
    let r1 = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &r1, None, "ca", 10));
    registrar(
        &mut l,
        "sa",
        Event::Secured {
            submission: a.clone(),
        },
        11,
    )
    .unwrap();
    builder(
        &mut l,
        "ra",
        Event::Reject {
            submission: a.clone(),
            reason: "grant missing".into(),
        },
        12,
    )
    .unwrap();
    // A new build asks the same question (same part, head and prefix).
    let r2 = match builder(&mut l, "q2", request("b2", 1, "p1"), 100) {
        Ok(Outcome::Request { id, .. }) => id,
        other => panic!("{other:?}"),
    };
    let r3 = match builder(&mut l, "q3", request("b2", 2, "other"), 100) {
        Ok(Outcome::Request { id, .. }) => id,
        other => panic!("{other:?}"),
    };
    assert!(matches!(
        registrar(
            &mut l,
            "c-bad",
            Event::Carry {
                from: a.clone(),
                to: r3
            },
            101
        ),
        Err(Refusal::ExpectationFailed(_))
    ));
    // While a submission of the target is open, a carry does not supersede it.
    let w2 = l
        .append(&ap(
            "writer-2",
            "kw",
            Event::Submit {
                request: r2.clone(),
                replaces: None,
                commit: "cw".into(),
            },
            102,
        ))
        .unwrap();
    assert!(matches!(
        registrar(
            &mut l,
            "c-open",
            Event::Carry {
                from: a.clone(),
                to: r2.clone()
            },
            103
        ),
        Err(Refusal::ExpectationFailed(_))
    ));
    let Outcome::Submission { id: w2 } = w2 else {
        panic!()
    };
    registrar(&mut l, "sw", Event::NeverSecured { submission: w2 }, 104).unwrap();
    let carried = sub_id(registrar(
        &mut l,
        "c-ok",
        Event::Carry {
            from: a.clone(),
            to: r2.clone(),
        },
        105,
    ));
    builder(
        &mut l,
        "adopt-c",
        Event::Adopt {
            submission: carried,
        },
        106,
    )
    .unwrap();
    // After an adoption a carry to the same request is refused.
    assert!(matches!(
        registrar(&mut l, "c-after", Event::Carry { from: a, to: r2 }, 107),
        Err(Refusal::Closed(_))
    ));
}

#[test]
fn a_carry_that_races_the_timeout_has_exactly_one_winner() {
    let mut l = ledger();
    let r1 = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &r1, None, "ca", 10));
    registrar(
        &mut l,
        "sa",
        Event::Secured {
            submission: a.clone(),
        },
        11,
    )
    .unwrap();
    builder(
        &mut l,
        "ra",
        Event::Reject {
            submission: a.clone(),
            reason: "r".into(),
        },
        12,
    )
    .unwrap();
    let r2 = match builder(&mut l, "q2", request("b2", 1, "p1"), 100) {
        Ok(Outcome::Request { id, .. }) => id,
        other => panic!("{other:?}"),
    };
    let at = 100 + DEADLINE_SECS + 1;
    builder(
        &mut l,
        "to",
        Event::TimedOut {
            request: r2.clone(),
        },
        at,
    )
    .unwrap();
    assert!(registrar(&mut l, "c", Event::Carry { from: a, to: r2 }, at).is_err());
}

#[test]
fn the_idempotency_key_is_scoped_and_bound_to_its_payload() {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let first = submit(&mut l, "same", &req, None, "ca", 10).unwrap();
    // The same key with another payload in one scope: refused, nothing dropped silently.
    assert_eq!(
        submit(&mut l, "same", &req, None, "other-commit", 11),
        Err(Refusal::KeyReuse)
    );
    // An exact retry after the state moved on returns the first result.
    let b = sub_id(submit(&mut l, "kb", &req, Some("s2"), "cb", 12));
    assert_ne!(b, "s2");
    assert_eq!(submit(&mut l, "same", &req, None, "ca", 13).unwrap(), first);
    // The same raw key under another principal, repository or verb is a new operation.
    let other_principal = l.append(&ap(
        "writer-2",
        "same",
        Event::Submit {
            request: req.clone(),
            replaces: None,
            commit: "ca".into(),
        },
        14,
    ));
    assert_eq!(
        other_principal,
        Err(Refusal::NotAllowed),
        "evaluated as its own operation, not answered from the first scope"
    );
    let mut other_repo = ap("builder", "same", request("b1", 9, "p9"), 15);
    other_repo.repo = "another/repo".into();
    assert!(matches!(l.append(&other_repo), Ok(Outcome::Request { .. })));
    let same_key_other_verb = builder(&mut l, "same", Event::TimedOut { request: req }, 16);
    assert!(same_key_other_verb.is_err() && same_key_other_verb != Err(Refusal::KeyReuse));
}

#[test]
fn a_service_failure_discard_names_the_open_pending_submission_of_this_build_past_the_bound() {
    let mut l = ledger();
    let req = open_request(&mut l, 0);
    let a = sub_id(submit(&mut l, "ka", &req, None, "ca", 10));
    let discard = |l: &mut Ledger, key: &str, build: &str, sub: &str, now: i64| {
        builder(
            l,
            key,
            Event::Discard {
                build: build.into(),
                reason: "service_failure".into(),
                submission: Some(sub.into()),
            },
            now,
        )
    };
    assert!(
        discard(&mut l, "d1", "b1", &a, 10 + PENDING_BOUND_SECS).is_err(),
        "younger than the bound"
    );
    assert!(
        discard(&mut l, "d2", "b2", &a, 20 + PENDING_BOUND_SECS).is_err(),
        "a submission of another build"
    );
    // A secured candidate is not blocked by the service.
    registrar(
        &mut l,
        "sa",
        Event::Secured {
            submission: a.clone(),
        },
        20,
    )
    .unwrap();
    assert!(discard(&mut l, "d3", "b1", &a, 20 + PENDING_BOUND_SECS).is_err());
    let b = {
        builder(
            &mut l,
            "rj",
            Event::Reject {
                submission: a,
                reason: "r".into(),
            },
            30,
        )
        .unwrap();
        sub_id(submit(&mut l, "kb", &req, None, "cb", 31))
    };
    discard(&mut l, "d4", "b1", &b, 31 + PENDING_BOUND_SECS + 1).unwrap();
    assert!(
        matches!(
            builder(&mut l, "q", request("b1", 2, "p"), 9000),
            Err(Refusal::Closed(_))
        ),
        "a build ends once"
    );
}

#[test]
fn a_verifier_replays_the_log_and_refuses_one_the_rules_would_not_have_accepted() {
    let writers = BTreeMap::from([("b1".to_owned(), "writer-1".to_owned())]);
    let mut keeper = Ledger::new(writers.clone());
    let steps = vec![
        ap("builder", "q", request("b1", 1, "p1"), 0),
        ap(
            "writer-1",
            "s1",
            Event::Submit {
                request: "r1".into(),
                replaces: None,
                commit: "c1".into(),
            },
            5,
        ),
        ap(
            "registrar",
            "sec",
            Event::Secured {
                submission: "s2".into(),
            },
            6,
        ),
        ap(
            "builder",
            "ad",
            Event::Adopt {
                submission: "s2".into(),
            },
            7,
        ),
    ];
    let log: Vec<_> = steps
        .into_iter()
        .map(|a| {
            let r = keeper.append(&a);
            (a, r)
        })
        .collect();
    assert!(log.iter().all(|(_, r)| r.is_ok()), "{log:?}");
    let replayed = replay(writers.clone(), &log).unwrap();
    assert_eq!(replayed.adopted("b1")[0].commit, "c1");
    // An adoption recorded as accepted before its candidate was secured is refused at its position.
    let mut forged = log.clone();
    let _ = forged.remove(2);
    assert_eq!(replay(writers.clone(), &forged).err(), Some(2));
    // A recorded outcome that differs from the replayed one is refused too.
    let mut changed = log;
    changed[1].1 = Ok(Outcome::Submission { id: "s9".into() });
    assert_eq!(replay(writers, &changed).err(), Some(1));
}
