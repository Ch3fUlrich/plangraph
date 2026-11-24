//! Red rounds, trees and restarts: the builder stops fixing in place after two red rounds and never
//! lands a tree CI did not run on.
mod support;
use pg_train_model::*;
use support::*;

fn started(parts: u8) -> Model {
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), disjoint(parts));
    register_all(&mut m, 0..parts);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    m
}

#[test]
fn after_two_red_rounds_the_builder_stops_fixing_in_place_and_drops_or_splits() {
    let mut m = started(4);
    fix_red(&mut m, 0);
    assert_eq!((m.train_ci(0), m.red_rounds(0)), (Ci::Red, 1));
    ok(&mut m, Event::FixInPlace { train: 0 });
    start(&mut m, 0);
    // The second red round: no fix in place, and the refusal changes nothing.
    assert_eq!(denied_fix_red(&mut m, 0), Denied::TwoRounds);
    assert_eq!(
        red(&mut m, 0, 2),
        Outcome::Red {
            groups: 1,
            escalated: true
        }
    );
    assert_eq!(m.status(2), Status::Dropped);
    assert_eq!(m.pending_groups(), [vec![0, 1, 3]]);
    // With nobody attributable it is split by stacks instead.
    let mut m = started(4);
    fix_red(&mut m, 0);
    ok(&mut m, Event::FixInPlace { train: 0 });
    start(&mut m, 0);
    assert_eq!(
        ci(&mut m, 0, false),
        Outcome::Red {
            groups: 2,
            escalated: true
        }
    );
    assert_eq!(m.pending_groups(), [vec![0, 1], vec![2, 3]]);
}

#[test]
fn an_open_red_train_can_be_split_or_dropped_from_without_a_second_run() {
    let mut m = started(3);
    fix_red(&mut m, 0);
    assert_eq!(
        red(&mut m, 0, 1),
        Outcome::Red {
            groups: 1,
            escalated: false
        }
    );
    assert!(m.train_closed(0));
}

#[test]
fn a_restart_keeps_the_red_round_count_and_the_running_ci() {
    let mut m = started(3);
    fix_red(&mut m, 0);
    ok(&mut m, Event::FixInPlace { train: 0 });
    start(&mut m, 0);
    ok(&mut m, Event::Restart);
    assert_eq!(m.red_rounds(0), 1);
    assert_eq!((m.train_ci(0), m.is_bound(0)), (Ci::Running, true));
    assert_eq!(denied_fix_red(&mut m, 0), Denied::TwoRounds);
}

#[test]
fn a_landing_whose_tree_differs_from_the_tested_tree_is_refused_until_ci_runs_again() {
    let mut m = started(2);
    ci(&mut m, 0, true);
    ok(&mut m, Event::Amend { train: 0 });
    assert_eq!(
        denied(&mut m, Event::Settle { train: 0 }),
        Denied::TreeChanged
    );
    // Green evidence is not carried to the changed tree: a rebuild needs a bind and a full run.
    ok(&mut m, Event::Rebuild { train: 0 });
    assert_eq!(m.train_ci(0), Ci::NotStarted);
    assert_eq!(denied(&mut m, Event::Settle { train: 0 }), Denied::NotGreen);
    start(&mut m, 0);
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(m.landed(), 2);
}

#[test]
fn a_fix_in_place_needs_a_fresh_bind_and_a_full_run_on_its_exact_tree() {
    let mut m = started(2);
    fix_red(&mut m, 0);
    ok(&mut m, Event::FixInPlace { train: 0 });
    assert_eq!(m.train_ci(0), Ci::NotStarted);
    assert_eq!(denied(&mut m, Event::StartCi { train: 0 }), Denied::Unbound);
    start(&mut m, 0);
    assert_eq!(m.train_ci(0), Ci::Running);
    // A replacement candidate (the retry group of the rest) starts its own full run.
    let mut m = started(3);
    red(&mut m, 0, 0);
    assert_eq!(ok(&mut m, Event::StartRetry), Outcome::Train(1));
    assert_eq!(m.train_ci(1), Ci::Running);
}

#[test]
fn the_blocked_descendants_of_a_dropped_part_leave_with_it_and_return_only_under_the_parent_rule() {
    let specs = vec![
        spec(1, None),
        spec(2, Some(0)),
        spec(4, None),
        spec(8, None),
    ];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, 0..3);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    red(&mut m, 0, 0);
    assert_eq!(
        (m.status(0), m.status(1)),
        (Status::Dropped, Status::Dropped)
    );
    ok(&mut m, Event::StartRetry);
    ci(&mut m, 1, true);
    ok(&mut m, Event::Settle { train: 1 });
    // The child is readmitted but waits: its parent has no verified landing.
    ok(&mut m, Event::Readmit { part: 1 });
    register_all(&mut m, [3]);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(2));
    assert_eq!(m.train_parts(2), [3]);
    assert_eq!(
        m.left_out(2),
        [(1, Disposition::ParentNotLanded { parent: 0 })]
    );
    land(&mut m, 2);
    // Readmitted, the parent rides first and the child with it, in one train: its parent is in the list.
    ok(&mut m, Event::Readmit { part: 0 });
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(3));
    assert_eq!(m.train_parts(3), [0, 1]);
}

#[test]
fn an_adopted_conflict_resolution_rejoins_before_ci_and_is_refused_after() {
    let specs = vec![spec(1, None), spec(2, Some(0)), spec(4, None)];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, 0..3);
    ok(&mut m, Event::Select);
    // A part that never conflicted has no resolution to adopt.
    assert_eq!(
        denied(&mut m, Event::Adopt { train: 0, part: 2 }),
        Denied::Phase
    );
    ok(&mut m, Event::Conflict { train: 0, part: 0 });
    assert_eq!(m.train_parts(0), [2]);
    // The resolution is adopted: the part and what waited on it ride again, unbound.
    ok(
        &mut m,
        Event::Bind {
            train: 0,
            build: 0,
            base: 0,
            policy: POLICY,
        },
    );
    ok(&mut m, Event::Adopt { train: 0, part: 0 });
    assert_eq!(m.train_parts(0), [2, 0, 1]);
    assert!(m.left_out(0).is_empty() && !m.is_bound(0));
    assert_eq!(
        denied(&mut m, Event::Adopt { train: 0, part: 0 }),
        Denied::Phase
    );
    // After the freeze a resolution is refused and the part stays out.
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), disjoint(2));
    register_all(&mut m, 0..2);
    ok(&mut m, Event::Select);
    ok(&mut m, Event::Conflict { train: 0, part: 0 });
    start(&mut m, 0);
    assert_eq!(
        denied(&mut m, Event::Adopt { train: 0, part: 0 }),
        Denied::CiStarted
    );
    assert_eq!(m.status(0), Status::Ready);
}

#[test]
fn an_adopted_resolution_leaves_out_a_descendant_whose_parent_was_dropped() {
    let specs = vec![
        spec(1, None),
        spec(2, Some(0)),
        spec(4, Some(1)),
        spec(8, None),
    ];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, 0..4);
    ok(&mut m, Event::Select);
    ok(&mut m, Event::Conflict { train: 0, part: 0 });
    ok(&mut m, Event::Drop { part: 1 });
    ok(&mut m, Event::Adopt { train: 0, part: 0 });
    // Part 2 waited on the dropped part 1: it stays out.
    assert_eq!(m.train_parts(0), [3, 0]);
    assert_eq!(m.status(2), Status::Ready);
}

#[test]
fn a_delayed_ci_callback_of_an_older_run_cannot_settle_a_rebuilt_train() {
    let mut m = started(2);
    assert_eq!(m.run(0), 1);
    ci(&mut m, 0, true);
    ok(&mut m, Event::Rebuild { train: 0 });
    start(&mut m, 0);
    assert_eq!(m.run(0), 2);
    // The green result of run 1 arrives late: refused, and the train is still waiting for run 2.
    let late = Event::CiResult {
        train: 0,
        run: 1,
        green: true,
    };
    assert_eq!(denied(&mut m, late), Denied::StaleRun);
    assert_eq!(
        denied(
            &mut m,
            Event::CiRed {
                train: 0,
                run: 1,
                culprit: 0
            }
        ),
        Denied::StaleRun
    );
    assert_eq!(
        denied(&mut m, Event::CiRedFix { train: 0, run: 1 }),
        Denied::StaleRun
    );
    assert_eq!(
        (m.train_ci(0), denied(&mut m, Event::Settle { train: 0 })),
        (Ci::Running, Denied::NotGreen)
    );
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
}

#[test]
fn ci_and_settlement_need_the_train_verified_complete_as_it_stands() {
    let mut m = Model::new(policy(1, 3, 0, Order::Oldest), disjoint(3));
    register_all(&mut m, 0..3);
    ok(&mut m, Event::Select);
    let base = m.main();
    ok(
        &mut m,
        Event::Bind {
            train: 0,
            build: 0,
            base,
            policy: POLICY,
        },
    );
    assert_eq!(
        denied(&mut m, Event::StartCi { train: 0 }),
        Denied::Unverified
    );
    // A reason the verifier rejects stops VerifyComplete too, and with it the CI.
    ok(
        &mut m,
        Event::RecordLeftOut {
            train: 0,
            part: 1,
            disposition: Disposition::Cap,
        },
    );
    assert_eq!(
        denied(&mut m, Event::VerifyComplete { train: 0 }),
        Denied::Disposition
    );
    assert_eq!(
        denied(&mut m, Event::StartCi { train: 0 }),
        Denied::Unverified
    );
    ok(
        &mut m,
        Event::RecordLeftOut {
            train: 0,
            part: 1,
            disposition: Disposition::Target,
        },
    );
    ok(&mut m, Event::VerifyComplete { train: 0 });
    ok(&mut m, Event::StartCi { train: 0 });
    ci(&mut m, 0, true);
    // A reason lost after the CI started unverifies the train: it cannot settle on that result.
    ok(&mut m, Event::OmitLeftOut { train: 0, part: 2 });
    assert_eq!(
        denied(&mut m, Event::Settle { train: 0 }),
        Denied::Unverified
    );
    assert_eq!(
        denied(&mut m, Event::VerifyComplete { train: 0 }),
        Denied::Disposition
    );
}

#[test]
fn the_red_rounds_of_the_whole_lineage_count_for_a_retry_group() {
    let mut m = started(3);
    red(&mut m, 0, 0);
    assert_eq!(ok(&mut m, Event::StartRetry), Outcome::Train(1));
    // The ordinary train was the first red round: the retry may not be fixed in place.
    assert_eq!(denied_fix_red(&mut m, 1), Denied::TwoRounds);
    assert_eq!(
        ci(&mut m, 1, false),
        Outcome::Red {
            groups: 2,
            escalated: true
        }
    );
    assert_eq!(m.escalated_lineages(), 1);
}
