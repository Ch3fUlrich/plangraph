mod support;
use pg_train_model::*;
use support::*;

#[test]
fn a_red_train_of_fourteen_runs_its_retry_group_of_seven_although_no_ordinary_part_is_due() {
    let mut specs = disjoint(14);
    specs.push(spec(1 << 14, None));
    let mut m = Model::new(policy(14, 3, 3, Order::Oldest), specs);
    register_all(&mut m, 0..14);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(0));
    assert_eq!(m.train_parts(0).len(), 14);
    start(&mut m, 0);
    assert_eq!(
        ok(
            &mut m,
            Event::CiResult {
                train: 0,
                green: false
            }
        ),
        Outcome::Red {
            groups: 2,
            escalated: false
        }
    );
    assert_eq!(
        m.pending_groups().iter().map(Vec::len).collect::<Vec<_>>(),
        [7, 7]
    );
    // One ordinary part is ready, below the size target and not waited long: not due.
    register_all(&mut m, [14]);
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    assert_eq!(ok(&mut m, Event::StartRetry), Outcome::Train(1));
    assert_eq!(m.train_parts(1).len(), 7);
    assert_eq!(m.train_ci(1), Ci::Running);
    assert_eq!(m.status(14), Status::Ready);
}

#[test]
fn retry_groups_keep_precedence_over_every_ordinary_selection() {
    let mut m = Model::new(policy(2, 2, 1, Order::Oldest), disjoint(5));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    ok(
        &mut m,
        Event::CiResult {
            train: 0,
            green: false,
        },
    );
    register_all(&mut m, [2, 3, 4]);
    ok(&mut m, Event::Tick);
    // Due by size and by age, yet the groups go first, then the running retry train.
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    ok(&mut m, Event::StartRetry);
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    ok(
        &mut m,
        Event::CiResult {
            train: 1,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 1 });
    assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
    ok(&mut m, Event::StartRetry);
    ok(
        &mut m,
        Event::CiResult {
            train: 2,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 2 });
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(3));
}

#[test]
fn the_second_red_round_of_a_lineage_escalates_once() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(4));
    register_all(&mut m, 0..4);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    assert_eq!(
        ok(
            &mut m,
            Event::CiResult {
                train: 0,
                green: false
            }
        ),
        Outcome::Red {
            groups: 2,
            escalated: false
        }
    );
    ok(&mut m, Event::StartRetry);
    assert_eq!(
        ok(
            &mut m,
            Event::CiResult {
                train: 1,
                green: false
            }
        ),
        Outcome::Red {
            groups: 2,
            escalated: true
        }
    );
    // A later red round of the same lineage does not escalate it again.
    ok(&mut m, Event::StartRetry);
    assert_eq!(
        ok(
            &mut m,
            Event::CiResult {
                train: 2,
                green: false
            }
        ),
        Outcome::Red {
            groups: 0,
            escalated: false
        }
    );
    assert_eq!(m.escalated_lineages(), 1);
}

#[test]
fn an_ordinary_part_waits_behind_a_red_train_for_at_most_two_n_minus_two_retry_runs() {
    // Part 4 is ready and oldest-but-for-two; the red train holds parts 0 and 1 (n = 2).
    let mut m = Model::new(policy(2, 2, 1, Order::Oldest), disjoint(6));
    register_all(&mut m, [0, 1, 4]);
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    assert_eq!(m.train_parts(0), [0, 1]);
    start(&mut m, 0);
    ok(
        &mut m,
        Event::CiResult {
            train: 0,
            green: false,
        },
    );
    let mut runs = 0;
    while !m.pending_groups().is_empty() {
        assert_eq!(denied(&mut m, Event::Select), Denied::RetryFirst);
        let Outcome::Train(t) = ok(&mut m, Event::StartRetry) else {
            panic!()
        };
        runs += 1;
        ok(
            &mut m,
            Event::CiResult {
                train: t,
                green: false,
            },
        );
    }
    assert_eq!(runs, 2);
    assert_eq!(m.retry_runs(1), 2);
    // The waiting part rides in the very next ordinary train.
    ok(&mut m, Event::Select);
    assert_eq!(m.train_parts(runs + 1), [4]);
}

#[test]
fn a_red_train_drops_the_failing_part_and_lands_the_rest() {
    // Part 1 fails; its child 2 drops with it; 0 and 3 land after one retry run, in a train of their own.
    let specs = vec![
        spec(1, None),
        spec(2, None),
        spec(4, Some(1)),
        spec(8, None),
    ];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, 0..4);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    assert_eq!(
        ok(
            &mut m,
            Event::CiRed {
                train: 0,
                culprit: 1
            }
        ),
        Outcome::Red {
            groups: 1,
            escalated: false
        }
    );
    assert_eq!(
        (m.status(1), m.status(2)),
        (Status::Dropped, Status::Dropped)
    );
    assert_eq!(m.pending_groups(), [vec![0, 3]]);
    assert_eq!(ok(&mut m, Event::StartRetry), Outcome::Train(1));
    ok(
        &mut m,
        Event::CiResult {
            train: 1,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 1 });
    assert_eq!(
        (m.status(0), m.status(3), m.landed()),
        (Status::Landed, Status::Landed, 2)
    );
    // A culprit that is not in the train, or a CI that is not running, is refused.
    ok(&mut m, Event::Readmit { part: 1 });
    ok(&mut m, Event::Select);
    assert_eq!(
        denied(
            &mut m,
            Event::CiRed {
                train: 2,
                culprit: 1
            }
        ),
        Denied::Phase
    );
    assert_eq!(
        denied(
            &mut m,
            Event::CiRed {
                train: 2,
                culprit: 0
            }
        ),
        Denied::Phase
    );
}

#[test]
fn a_culprit_that_is_the_only_part_leaves_no_retry_group() {
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), disjoint(2));
    register_all(&mut m, [0]);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    assert_eq!(
        ok(
            &mut m,
            Event::CiRed {
                train: 0,
                culprit: 0
            }
        ),
        Outcome::Red {
            groups: 0,
            escalated: false
        }
    );
    assert_eq!(m.status(0), Status::Dropped);
    assert!(m.pending_groups().is_empty());
}
