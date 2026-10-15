use super::*;
use ExitClass::*;

const T0: SystemTime = SystemTime::UNIX_EPOCH;

fn at(class: ExitClass, leg: &str, tier: Tier) -> Attempt {
    Attempt {
        exit_class: class,
        leg: leg.into(),
        tier,
        diff_hash: "d0".into(),
        check_hash: "c0".into(),
        context_overflow: false,
    }
}

fn varied(mut a: Attempt, n: u32) -> Attempt {
    a.diff_hash = format!("d{n}");
    a.check_hash = format!("c{n}");
    a
}

fn leg(id: &str, tier: Tier, limited: bool) -> Leg {
    Leg {
        id: id.into(),
        tier,
        limited,
    }
}

fn run(history: &[Attempt], legs: &[Leg], ctx: ContextClass, waits: u32, jitter: f64) -> Action {
    let inp = Input {
        card: "card-1",
        history,
        legs,
        context: ctx,
        waits,
        now: T0,
        jitter,
        check_tail: "tail",
    };
    next_action(&Config::default(), &inp)
}

fn simple(history: &[Attempt]) -> Action {
    run(history, &[], ContextClass::K128, 0, 0.0)
}

fn park(tier: Tier, class: ExitClass) -> Action {
    Action::Park {
        packet: ParkPacket {
            card: "card-1".into(),
            tier,
            exit_class: class,
            check_tail: "tail".into(),
        },
    }
}

/// n distinct (non-quarantining) failures of `class` on `tier`.
fn fails(class: ExitClass, tier: Tier, n: u32) -> Vec<Attempt> {
    (0..n).map(|i| varied(at(class, "l", tier), i)).collect()
}

#[test]
fn terminal_and_routing_rules() {
    let cases: Vec<(&str, Vec<Attempt>, Action)> = vec![
        ("empty history", vec![], Action::Retry),
        ("ok", vec![at(Ok, "l", Tier::Free)], Action::Done),
        (
            "contract defect",
            vec![at(ContractDefect, "l", Tier::Free)],
            Action::ToPlanner,
        ),
        (
            "flaky check",
            vec![at(FlakyCheck, "l", Tier::Free)],
            Action::Quarantine,
        ),
        (
            "cancelled",
            vec![at(Cancelled, "l", Tier::Free)],
            park(Tier::Free, Cancelled),
        ),
        (
            "needs input",
            vec![at(NeedsInput, "l", Tier::Paid)],
            park(Tier::Paid, NeedsInput),
        ),
    ];
    for (name, h, want) in cases {
        assert_eq!(simple(&h), want, "{name}");
    }
}

#[test]
fn retryable_classes_retry_until_budget() {
    for class in [
        CheckFail,
        WorkerCrash,
        Stall,
        Timeout,
        LeaseLost,
        RebaseConflict,
        MergeRefused,
    ] {
        for n in 1..=2 {
            assert_eq!(
                simple(&fails(class, Tier::Trial, n)),
                Action::Retry,
                "{class:?} x{n}"
            );
        }
        assert_eq!(
            simple(&fails(class, Tier::Trial, 3)),
            Action::Promote { tier: Tier::Free },
            "{class:?} x3"
        );
    }
}

#[test]
fn promotion_ladder_and_single_promotion() {
    // 3 on trial -> promote to free
    assert_eq!(
        simple(&fails(WorkerCrash, Tier::Trial, 3)),
        Action::Promote { tier: Tier::Free }
    );
    // after promotion: fresh budget of 3 on free
    let mut h = fails(WorkerCrash, Tier::Trial, 3);
    h.push(varied(at(WorkerCrash, "l", Tier::Free), 10));
    assert_eq!(simple(&h), Action::Retry);
    h.push(varied(at(WorkerCrash, "l", Tier::Free), 11));
    assert_eq!(simple(&h), Action::Retry);
    // 3rd on free: promotion already used -> park
    h.push(varied(at(WorkerCrash, "l", Tier::Free), 12));
    assert_eq!(simple(&h), park(Tier::Free, WorkerCrash));
    // each starting tier promotes to the next, except paid
    for (from, to) in [
        (Tier::Free, Some(Tier::Subscription)),
        (Tier::Subscription, Some(Tier::Paid)),
        (Tier::Paid, None),
    ] {
        let want = to.map_or(park(from, Timeout), |tier| Action::Promote { tier });
        assert_eq!(simple(&fails(Timeout, from, 3)), want, "{from:?}");
    }
}

#[test]
fn limits_and_outages_do_not_consume_attempts() {
    let mut h = fails(CheckFail, Tier::Trial, 2);
    for _ in 0..5 {
        h.push(at(UsageLimit, "a", Tier::Trial));
        h.push(at(Outage, "a", Tier::Trial));
    }
    // legs: a limited, b free -> reroute to b, not parked/promoted
    let legs = [leg("a", Tier::Trial, true), leg("b", Tier::Trial, false)];
    assert_eq!(
        run(&h, &legs, ContextClass::K128, 0, 0.0),
        Action::Reroute { leg: "b".into() }
    );
    // and a real failure afterwards is only the 3rd counted attempt
    let mut h2 = h.clone();
    h2.push(varied(at(CheckFail, "b", Tier::Trial), 9));
    assert_eq!(simple(&h2), Action::Promote { tier: Tier::Free });
}

#[test]
fn reroute_picks_next_unlimited_leg_of_same_tier() {
    let legs = [
        leg("a", Tier::Free, false),
        leg("b", Tier::Free, true),
        leg("c", Tier::Free, false),
        leg("z", Tier::Trial, false),
    ];
    for class in [UsageLimit, Outage] {
        // a failed -> skip limited b -> c
        let h = [at(class, "a", Tier::Free)];
        assert_eq!(
            run(&h, &legs, ContextClass::K128, 0, 0.0),
            Action::Reroute { leg: "c".into() }
        );
        // c failed -> wraps to a (never to other tiers)
        let h = [at(class, "c", Tier::Free)];
        assert_eq!(
            run(&h, &legs, ContextClass::K128, 0, 0.0),
            Action::Reroute { leg: "a".into() }
        );
    }
}

#[test]
fn wait_only_when_all_legs_of_tier_limited() {
    let h = [at(UsageLimit, "a", Tier::Free)];
    let all = [leg("a", Tier::Free, true), leg("b", Tier::Free, true)];
    let Action::Wait { until } = run(&h, &all, ContextClass::K128, 0, 1.0) else {
        panic!("expected wait");
    };
    assert_eq!(until, T0 + Duration::from_secs(60));
    // the failing leg counts as limited even if the caller did not flag it
    let one = [leg("a", Tier::Free, false), leg("b", Tier::Free, true)];
    assert!(matches!(
        run(&h, &one, ContextClass::K128, 0, 0.5),
        Action::Wait { .. }
    ));
    // single-leg tier and no legs at all wait too
    let solo = [leg("a", Tier::Free, false)];
    assert!(matches!(
        run(&h, &solo, ContextClass::K128, 0, 0.5),
        Action::Wait { .. }
    ));
    assert!(matches!(
        run(&h, &[], ContextClass::K128, 0, 0.5),
        Action::Wait { .. }
    ));
}

#[test]
fn wait_is_exponential_jittered_and_capped() {
    let h = [at(Outage, "a", Tier::Free)];
    let legs = [leg("a", Tier::Free, true)];
    let secs = |waits, jitter| match run(&h, &legs, ContextClass::K128, waits, jitter) {
        Action::Wait { until } => until.duration_since(T0).unwrap().as_secs_f64(),
        other => panic!("{other:?}"),
    };
    // jitter 1.0 -> full step, 0.0 -> half step
    assert_eq!(secs(0, 1.0), 60.0);
    assert_eq!(secs(1, 1.0), 120.0);
    assert_eq!(secs(3, 1.0), 480.0);
    assert_eq!(secs(0, 0.0), 30.0);
    assert_eq!(secs(2, 0.0), 120.0);
    // cap at 24h, never above, even for absurd counts
    assert_eq!(secs(30, 1.0), 86_400.0);
    assert_eq!(secs(1000, 1.0), 86_400.0);
    assert_eq!(secs(1000, 0.0), 43_200.0);
    // out-of-range jitter is clamped
    assert_eq!(secs(0, 7.0), 60.0);
}

#[test]
fn probe_fail_backs_off_exponentially() {
    let delay = |n: usize, jitter| match run(
        &vec![at(ProbeFail, "l", Tier::Free); n],
        &[],
        ContextClass::K128,
        0,
        jitter,
    ) {
        Action::Backoff { delay } => delay.as_secs_f64(),
        other => panic!("{other:?}"),
    };
    assert_eq!(delay(1, 1.0), 30.0);
    assert_eq!(delay(2, 1.0), 60.0);
    assert_eq!(delay(3, 0.0), 60.0);
    assert_eq!(delay(100, 1.0), 86_400.0);
}

#[test]
fn context_upgrade_only_on_overflow() {
    let overflow = |ctx| {
        let mut a = at(WorkerCrash, "l", Tier::Free);
        a.context_overflow = true;
        run(&[a], &[], ctx, 0, 0.0)
    };
    assert_eq!(
        overflow(ContextClass::K128),
        Action::UpgradeClass {
            class: ContextClass::K256
        }
    );
    assert_eq!(
        overflow(ContextClass::K256),
        Action::UpgradeClass {
            class: ContextClass::M1
        }
    );
    assert_eq!(overflow(ContextClass::M1), park(Tier::Free, WorkerCrash));
    // no overflow: never upgrades, whatever the class
    for class in [CheckFail, WorkerCrash, Stall, Timeout] {
        let a = [varied(at(class, "l", Tier::Free), 1)];
        assert!(!matches!(
            run(&a, &[], ContextClass::K128, 0, 0.0),
            Action::UpgradeClass { .. }
        ));
    }
    assert_eq!(ContextClass::M1.next(), None);
}

#[test]
fn quarantine_on_identical_failures_with_unchanged_diff() {
    let same = vec![
        at(CheckFail, "l", Tier::Free),
        at(CheckFail, "l", Tier::Free),
    ];
    assert_eq!(simple(&same), Action::Quarantine);
    // diff changed
    let mut d = same.clone();
    d[1].diff_hash = "other".into();
    assert_eq!(simple(&d), Action::Retry);
    // failure output changed
    let mut c = same.clone();
    c[1].check_hash = "other".into();
    assert_eq!(simple(&c), Action::Retry);
    // different class in between
    let m = vec![
        at(CheckFail, "l", Tier::Free),
        at(WorkerCrash, "l", Tier::Free),
    ];
    assert_eq!(simple(&m), Action::Retry);
    // limits between the two failures do not hide them
    let l = vec![
        at(CheckFail, "l", Tier::Free),
        at(UsageLimit, "l", Tier::Free),
        at(CheckFail, "l", Tier::Free),
    ];
    assert_eq!(simple(&l), Action::Quarantine);
    // a single failure never quarantines
    assert_eq!(simple(&same[..1]), Action::Retry);
}

#[test]
fn park_packet_carries_card_tier_class_and_tail() {
    let h = fails(CheckFail, Tier::Paid, 3);
    let Action::Park { packet } = simple(&h) else {
        panic!("expected park")
    };
    assert_eq!(packet.card, "card-1");
    assert_eq!(packet.tier, Tier::Paid);
    assert_eq!(packet.exit_class, CheckFail);
    assert_eq!(packet.check_tail, "tail");
}

#[test]
fn action_serializes_with_tag() {
    let v = serde_json::to_value(Action::Promote { tier: Tier::Free }).unwrap();
    assert_eq!(v["action"], "promote");
    assert_eq!(v["tier"], "free");
    let j = serde_json::to_value(Action::Retry).unwrap();
    assert_eq!(j["action"], "retry");
}

#[test]
fn random_jitter_in_unit_interval() {
    for _ in 0..100 {
        let j = random_jitter();
        assert!((0.0..1.0).contains(&j));
    }
}
