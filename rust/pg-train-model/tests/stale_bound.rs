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
    ok(m, Event::MainMove { paths: 0 });
    ok(m, Event::Rebuild { train: 1 });
    start(m, 1);
}

#[test]
fn in_tested_ff_a_retry_train_stale_twice_lands_on_the_current_main_with_no_third_rebuild() {
    let mut m = retrying(Policy::every_ready_rides(Order::Oldest));
    stale_once(&mut m);
    stale_once(&mut m);
    assert_eq!((m.run(1), m.rebuilds(1)), (3, 2));
    // The green result of the third run is there to land when main moves again.
    ci(&mut m, 1, true);
    ok(&mut m, Event::MainMove { paths: 0 });
    assert_eq!(
        denied(&mut m, Event::Rebuild { train: 1 }),
        Denied::StaleBound
    );
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    // The tested train lands on the main that moved; the third run never happens.
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
    ok(&mut m, Event::MainMove { paths: 0 });
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
    ok(&mut m, Event::MainMove { paths: 0 });
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

#[test]
fn a_stale_landing_is_refused_when_main_gained_a_change_to_the_trains_own_paths() {
    let mut m = retrying(Policy::every_ready_rides(Order::Oldest));
    stale_once(&mut m);
    stale_once(&mut m);
    // Part 1 owns path bit 1. A move of main that changes it makes the tested tree not what a
    // fast-forward of the current main would hold: no stale landing, and the rebuild is not refused.
    ok(&mut m, Event::MainMove { paths: 0b10 });
    ci(&mut m, 1, true);
    assert_eq!(denied(&mut m, Event::Settle { train: 1 }), Denied::Stale);
    ok(&mut m, Event::Rebuild { train: 1 });
    start(&mut m, 1);
    ci(&mut m, 1, true);
    ok(&mut m, Event::Settle { train: 1 });
    assert_eq!((m.rebuilds(1), m.stale_landings()), (3, 0));
}

#[test]
fn only_rebuilds_caused_by_a_stale_base_count_toward_the_bound() {
    let mut m = retrying(Policy::every_ready_rides(Order::Oldest));
    // Three rebuilds on the same base: nothing moved, nothing counts.
    for _ in 0..3 {
        ok(&mut m, Event::Rebuild { train: 1 });
        start(&mut m, 1);
    }
    assert_eq!((m.rebuilds(1), m.run(1)), (0, 4));
    stale_once(&mut m);
    assert_eq!(m.rebuilds(1), 1);
    // Not yet rebuilt twice for a stale base, so it cannot land stale.
    ok(&mut m, Event::MainMove { paths: 0 });
    ci(&mut m, 1, true);
    assert_eq!(denied(&mut m, Event::Settle { train: 1 }), Denied::Stale);
}

#[test]
fn in_pr_merge_mode_the_ordinary_train_and_the_queued_groups_go_on_after_the_retry_closes() {
    let mut p = Policy::every_ready_rides(Order::Oldest);
    p.landing = Landing::PrMerge;
    let mut m = Model::new(p, disjoint(5));
    register_all(&mut m, 0..3);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    ci(&mut m, 0, false);
    // Two groups: [0, 1] runs, [2] is queued.
    assert_eq!(m.pending_groups(), [vec![0, 1], vec![2]]);
    assert_eq!(ok(&mut m, Event::StartRetry), Outcome::Train(1));
    register_all(&mut m, [3]);
    stale_once(&mut m);
    stale_once(&mut m);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(2));
    // The retry train closes while the ordinary train is open and a group is queued.
    ci(&mut m, 1, true);
    ok(&mut m, Event::Settle { train: 1 });
    assert_eq!(m.pending_groups(), [vec![2]]);
    // The queued group waits for the ordinary train; it lands or goes red first.
    assert_eq!(denied(&mut m, Event::StartRetry), Denied::Phase);
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    ok(&mut m, Event::Rebuild { train: 2 });
    start(&mut m, 2);
    ci(&mut m, 2, true);
    ok(&mut m, Event::Settle { train: 2 });
    assert_eq!(ok(&mut m, Event::StartRetry), Outcome::Train(3));
    assert_eq!(m.train_parts(3), [2]);
}

#[test]
fn main_moving_before_the_bind_or_before_a_green_result_does_not_strand_the_retry_train() {
    let mut m = retrying(Policy::every_ready_rides(Order::Oldest));
    stale_once(&mut m);
    // The second rebuild is made but not bound yet when main moves again.
    ok(&mut m, Event::MainMove { paths: 0 });
    ok(&mut m, Event::Rebuild { train: 1 });
    ok(&mut m, Event::MainMove { paths: 0 });
    let stale = Event::Bind {
        train: 1,
        build: 1,
        base: m.main() - 1,
        policy: POLICY,
    };
    assert_eq!(denied(&mut m, stale), Denied::Stale);
    // No green result exists to land: the rebuild is allowed, not refused as a third.
    ok(&mut m, Event::Rebuild { train: 1 });
    start(&mut m, 1);
    ci(&mut m, 1, true);
    ok(&mut m, Event::Settle { train: 1 });
    assert_eq!(m.landed(), 1);
}
