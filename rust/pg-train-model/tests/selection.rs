mod support;
use pg_train_model::*;
use support::*;

#[test]
fn an_overlapping_part_is_deferred_only_until_its_bound_then_rides() {
    let specs = vec![spec(0b1, None), spec(0b1, None), spec(0b10, None)];
    let run = |ticks: u8| {
        let mut m = Model::new(policy(4, 2, 1, Order::Oldest), specs.clone());
        register_all(&mut m, [0, 1, 2]);
        (0..ticks).for_each(|_| {
            ok(&mut m, Event::Tick);
        });
        ok(&mut m, Event::Select);
        m
    };
    let early = run(1);
    assert_eq!(early.train_parts(0), [0, 2]);
    assert_eq!(
        early.left_out(0),
        [(1, Disposition::Overlap { with: 0, waited: 1 })]
    );
    let late = run(2);
    assert_eq!(late.train_parts(0), [0, 1, 2]);
    assert!(late.left_out(0).is_empty());
}

#[test]
fn by_default_every_ready_part_rides_and_only_a_missing_parent_holds_one_back() {
    // Four parts overlapping on one path, and a fifth that is the child of a dropped part.
    let specs = vec![
        spec(1, None),
        spec(1, None),
        spec(1, None),
        spec(1, Some(4)),
        spec(1, None),
    ];
    let mut m = Model::new(Policy::every_ready_rides(Order::Count), specs);
    register_all(&mut m, [4]);
    ok(&mut m, Event::Drop { part: 4 });
    register_all(&mut m, [3, 0, 1, 2]);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(0));
    assert_eq!(m.train_parts(0).len(), 3);
    assert_eq!(
        m.left_out(0),
        [(3, Disposition::ParentNotLanded { parent: 4 })]
    );
    // No deferral, no size target, no wait: a single ready part makes a train at once.
    let specs = vec![spec(1, None), spec(1, Some(1)), spec(1, None)];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, [2]);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(0));
    assert_eq!(m.train_parts(0), [2]);
}

fn one_path(n: u8) -> Vec<PartSpec> {
    (0..n).map(|_| spec(1, None)).collect()
}

/// Cut, test and land the next train; returns its parts.
fn cycle(m: &mut Model) -> Vec<PartId> {
    let Outcome::Train(t) = ok(m, Event::Select) else {
        panic!()
    };
    let parts = m.train_parts(t).to_vec();
    land(m, t);
    parts
}

#[test]
fn nine_independent_parts_on_one_path_land_as_one_one_and_seven_under_policy_a() {
    let sizes = |order| {
        let mut m = Model::new(policy(9, 200, 0, order), one_path(9));
        register_all(&mut m, 0..9);
        let mut sizes = vec![];
        while m.landed() < 9 {
            sizes.push(cycle(&mut m).len());
        }
        sizes
    };
    // (a) a part left out twice is due and goes first; (b) takes one part per train until the bound.
    assert_eq!(sizes(Order::Count), [1, 1, 7]);
    assert_eq!(sizes(Order::Oldest), [1; 9]);
}

#[test]
fn a_hundred_and_one_due_parts_give_a_train_of_one_hundred_and_one_cap() {
    let specs: Vec<PartSpec> = (0..101).map(|_| spec(0, None)).collect();
    let mut p = Policy::every_ready_rides(Order::Oldest);
    p.cap = 100;
    let mut m = Model::new(p, specs);
    register_all(&mut m, 0..101);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(0));
    assert_eq!(m.train_parts(0).len(), 100);
    assert_eq!(m.left_out(0), [(100, Disposition::Cap)]);
    let verify = Event::VerifyLeftOut {
        train: 0,
        part: 100,
    };
    assert_eq!(ok(&mut m, verify.clone()), Outcome::Accepted);
    // Below the limit the same reason is refused.
    ok(&mut m, Event::Drop { part: 3 });
    assert_eq!(denied(&mut m, verify), Denied::Disposition);
}

#[test]
fn a_ready_part_with_no_place_and_no_reason_fails_verification() {
    let mut m = Model::new(policy(1, 3, 0, Order::Oldest), disjoint(3));
    register_all(&mut m, 0..3);
    ok(&mut m, Event::Select);
    assert_eq!(m.left_out(0).len(), 2);
    assert_eq!(
        ok(&mut m, Event::VerifyComplete { train: 0 }),
        Outcome::Accepted
    );
    ok(&mut m, Event::OmitLeftOut { train: 0, part: 2 });
    assert_eq!(
        denied(&mut m, Event::VerifyComplete { train: 0 }),
        Denied::Disposition
    );
    // A part that moved on (dropped) needs no reason from this train.
    ok(&mut m, Event::Drop { part: 2 });
    assert_eq!(
        ok(&mut m, Event::VerifyComplete { train: 0 }),
        Outcome::Accepted
    );
}

#[test]
fn a_restart_keeps_the_deferral_age_and_asks_an_open_train_to_bind_again() {
    let mut m = Model::new(policy(9, 200, 0, Order::Count), one_path(3));
    register_all(&mut m, 0..3);
    ok(&mut m, Event::Tick);
    cycle(&mut m);
    let before = (m.deferrals(1), m.waited(1));
    assert_eq!(before, (1, 1));
    ok(&mut m, Event::Select);
    let base = m.main();
    ok(
        &mut m,
        Event::Bind {
            train: 1,
            build: 1,
            base,
            policy: POLICY,
        },
    );
    assert!(m.is_bound(1));
    ok(&mut m, Event::Restart);
    assert_eq!((m.deferrals(2), m.waited(2)), (2, 1));
    assert!(!m.is_bound(1));
    assert_eq!(denied(&mut m, Event::StartCi { train: 1 }), Denied::Unbound);
}

#[test]
fn the_child_of_a_reverted_part_is_refused_at_registration_and_at_build() {
    let specs = vec![
        spec(1, None),
        spec(2, Some(0)),
        spec(4, Some(0)),
        spec(8, None),
    ];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, [0]);
    ok(&mut m, Event::Select);
    land(&mut m, 0);
    register_all(&mut m, [1]);
    ok(&mut m, Event::Revert { part: 0 });
    // At registration: the parent was reverted.
    assert_eq!(
        denied(&mut m, Event::Register { part: 2 }),
        Denied::Dependency
    );
    // At build: the ready child has no verified parent and is left out.
    register_all(&mut m, [3]);
    ok(&mut m, Event::Select);
    assert_eq!(
        m.left_out(1),
        [(1, Disposition::ParentNotLanded { parent: 0 })]
    );
    assert_eq!(
        ok(&mut m, Event::VerifyLeftOut { train: 1, part: 1 }),
        Outcome::Accepted
    );
}
