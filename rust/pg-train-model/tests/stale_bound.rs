//! D-6840 (experiment): a retry train stale k = 2 times. In tested fast-forward it lands on the current
//! main with no third rebuild; in pull request merge mode an ordinary selection runs beside it.
mod support;
use pg_train_model::*;
use support::*;

/// Part 0 went red and dropped, part 1 retries alone; part 3 is an ordinary part waiting.
fn retrying(policy: Policy) -> Model {
    let mut m = Model::new(policy, disjoint(4));
    register_all(&mut m, 0..2);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    red(&mut m, 0, 0);
    register_all(&mut m, [3]);
    assert_eq!(ok(&mut m, Event::StartRetry), Outcome::Train(1));
    m
}

/// Main moves; the stale retry train is rebuilt and tested again.
fn stale_once(m: &mut Model) {
    ok(m, Event::MainMove);
    ok(m, Event::Rebuild { train: 1 });
    start(m, 1);
}

#[test]
fn in_tested_ff_a_retry_train_stale_twice_lands_on_the_current_main_with_no_third_rebuild() {
    let mut m = retrying(Policy::every_ready_rides(Order::Oldest));
    stale_once(&mut m);
    stale_once(&mut m);
    assert_eq!((m.run(1), m.rebuilds(1)), (3, 2));
    ok(&mut m, Event::MainMove);
    assert_eq!(
        denied(&mut m, Event::Rebuild { train: 1 }),
        Denied::StaleBound
    );
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    // The tested train lands on the main that moved; the third run never happens.
    ci(&mut m, 1, true);
    ok(&mut m, Event::Settle { train: 1 });
    assert_eq!((m.run(1), m.stale_landings()), (3, 1));
    // The ordinary part waited behind exactly the two rebuilds, then rides.
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(2));
    assert_eq!(m.train_parts(2), [3]);
}

#[test]
fn before_the_second_rebuild_a_stale_retry_train_still_cannot_land() {
    let mut m = retrying(Policy::every_ready_rides(Order::Oldest));
    stale_once(&mut m);
    ok(&mut m, Event::MainMove);
    ci(&mut m, 1, true);
    assert_eq!(denied(&mut m, Event::Settle { train: 1 }), Denied::Stale);
}

#[test]
fn in_pr_merge_mode_an_ordinary_selection_runs_beside_the_retry_train_after_two_rebuilds() {
    let mut p = Policy::every_ready_rides(Order::Oldest);
    p.landing = Landing::PrMerge;
    let mut m = retrying(p);
    stale_once(&mut m);
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    stale_once(&mut m);
    // Two rebuilds: the ordinary part no longer waits for the retry train.
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(2));
    assert_eq!(m.train_parts(2), [3]);
    assert!(m.invariants_hold());
    // The retry train is still rebuilt on a moved main here: the bound is the ordinary part's.
    ok(&mut m, Event::MainMove);
    ok(&mut m, Event::Rebuild { train: 1 });
    assert_eq!((m.rebuilds(1), m.stale_landings()), (3, 0));
}

#[test]
fn the_plain_per_group_bound_is_one_switch_away() {
    let mut m = retrying(Policy::every_ready_rides(Order::Oldest).without_stale_bound());
    for _ in 0..3 {
        stale_once(&mut m);
    }
    assert_eq!((m.run(1), m.stale_landings()), (4, 0));
}
