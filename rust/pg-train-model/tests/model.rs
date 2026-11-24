mod support;
use pg_train_model::*;
use support::*;

fn disjoint(n: u8) -> Vec<PartSpec> {
    (0..n).map(|i| spec(1 << i, None)).collect()
}

#[test]
fn no_part_joins_a_train_whose_ci_has_started() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(4));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
    assert_eq!(ok(&mut m, Event::Select), Outcome::Train(0));
    register_all(&mut m, [2]);
    ok(&mut m, Event::Join { part: 2, train: 0 });
    assert_eq!(m.train_parts(0), [0, 1, 2]);
    start(&mut m, 0);
    register_all(&mut m, [3]);
    assert_eq!(
        denied(&mut m, Event::Join { part: 3, train: 0 }),
        Denied::CiStarted
    );
    // The late part waits for the next train; the sealed one lands unchanged.
    ok(
        &mut m,
        Event::CiResult {
            train: 0,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(m.status(3), Status::Ready);
    assert_eq!(m.status(2), Status::Landed);
}

#[test]
fn a_join_after_a_bind_unbinds_the_train() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(3));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
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
    register_all(&mut m, [2]);
    ok(&mut m, Event::Join { part: 2, train: 0 });
    assert_eq!(denied(&mut m, Event::StartCi { train: 0 }), Denied::Unbound);
}

#[test]
fn the_binding_refuses_a_foreign_build_base_or_policy() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(2));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    let base = m.main();
    let bind = |build, base, policy| Event::Bind {
        train: 0,
        build,
        base,
        policy,
    };
    assert_eq!(denied(&mut m, bind(1, base, POLICY)), Denied::BindMismatch);
    assert_eq!(
        denied(&mut m, bind(0, base + 1, POLICY)),
        Denied::BindMismatch
    );
    assert_eq!(
        denied(&mut m, bind(0, base, POLICY + 1)),
        Denied::BindMismatch
    );
    assert_eq!(denied(&mut m, Event::StartCi { train: 0 }), Denied::Unbound);
    ok(&mut m, bind(0, base, POLICY));
}

#[test]
fn a_train_built_on_a_moved_main_cannot_bind_or_settle_until_rebuilt() {
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), disjoint(2));
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    ok(
        &mut m,
        Event::CiResult {
            train: 0,
            green: true,
        },
    );
    ok(&mut m, Event::MainMove);
    assert_eq!(denied(&mut m, Event::Settle { train: 0 }), Denied::Stale);
    ok(&mut m, Event::Rebuild { train: 0 });
    assert_eq!(m.train_ci(0), Ci::NotStarted);
    assert_eq!(denied(&mut m, Event::Settle { train: 0 }), Denied::NotGreen);
    start(&mut m, 0);
    ok(
        &mut m,
        Event::CiResult {
            train: 0,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(m.main(), 2);
}

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
fn the_two_orders_differ_in_how_many_disjoint_parts_ride() {
    // Part 0 overlaps both 1 and 2, which are disjoint from each other.
    let specs = vec![spec(0b11, None), spec(0b01, None), spec(0b10, None)];
    let pick = |order| {
        let mut m = Model::new(policy(3, 3, 1, order), specs.clone());
        register_all(&mut m, [0, 1, 2]);
        ok(&mut m, Event::Select);
        m.train_parts(0).to_vec()
    };
    assert_eq!(pick(Order::Oldest), [0]);
    assert_eq!(pick(Order::Count), [1, 2]);
}

#[test]
fn a_conflicting_part_and_its_children_leave_an_open_train_and_a_dropped_part_can_be_readmitted() {
    let specs = vec![spec(1, None), spec(2, Some(0)), spec(4, None)];
    let mut m = Model::new(policy(3, 2, 1, Order::Oldest), specs);
    register_all(&mut m, [0, 1, 2]);
    ok(&mut m, Event::Select);
    assert_eq!(m.train_parts(0), [0, 1, 2]);
    ok(&mut m, Event::Conflict { train: 0, part: 0 });
    assert_eq!(m.train_parts(0), [2]);
    assert_eq!((m.status(0), m.status(1)), (Status::Ready, Status::Ready));
    ok(&mut m, Event::Drop { part: 0 });
    assert_eq!(m.status(0), Status::Dropped);
    start(&mut m, 0);
    assert_eq!(
        denied(&mut m, Event::Conflict { train: 0, part: 2 }),
        Denied::Phase
    );
    ok(&mut m, Event::Readmit { part: 0 });
    assert_eq!(m.status(0), Status::Ready);
    assert_eq!(denied(&mut m, Event::Readmit { part: 0 }), Denied::Phase);
}

#[test]
fn a_revert_takes_the_landed_children_with_it_and_moves_main() {
    let specs = vec![spec(1, None), spec(2, Some(0))];
    let mut m = Model::new(policy(2, 2, 1, Order::Oldest), specs);
    register_all(&mut m, [0, 1]);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    ok(
        &mut m,
        Event::CiResult {
            train: 0,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(m.main(), 1);
    ok(&mut m, Event::Revert { part: 0 });
    assert_eq!(
        (m.status(0), m.status(1), m.main()),
        (Status::Dropped, Status::Dropped, 2)
    );
    assert_eq!(denied(&mut m, Event::Revert { part: 0 }), Denied::Phase);
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

#[test]
fn dropping_the_root_of_a_three_part_stack_frees_every_part_above_it_from_the_retry_groups() {
    // The red single stack 0 <- 1 <- 2 is cut at a closed prefix: [0, 1] and [2].
    let specs = vec![spec(1, None), spec(2, Some(0)), spec(4, Some(1))];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, 0..3);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    ok(
        &mut m,
        Event::CiResult {
            train: 0,
            green: false,
        },
    );
    assert_eq!(m.pending_groups(), [vec![0, 1], vec![2]]);
    ok(&mut m, Event::Drop { part: 0 });
    assert_eq!((m.status(1), m.status(2)), (Status::Ready, Status::Ready));
    assert!(m.pending_groups().is_empty());
}

#[test]
fn a_revert_frees_held_descendants_at_every_depth() {
    let specs = vec![
        spec(1, None),
        spec(2, Some(0)),
        spec(4, Some(1)),
        spec(8, Some(2)),
    ];
    let mut m = Model::new(Policy::every_ready_rides(Order::Oldest), specs);
    register_all(&mut m, 0..4);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    ok(
        &mut m,
        Event::CiRed {
            train: 0,
            culprit: 3,
        },
    );
    ok(&mut m, Event::StartRetry);
    ok(
        &mut m,
        Event::CiResult {
            train: 1,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 1 });
    assert_eq!(m.landed(), 3);
    ok(&mut m, Event::Revert { part: 0 });
    assert_eq!(m.landed(), 0);
    assert!(m.pending_groups().is_empty());
}
