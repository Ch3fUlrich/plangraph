mod support;
use pg_train_model::*;
use support::*;

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
