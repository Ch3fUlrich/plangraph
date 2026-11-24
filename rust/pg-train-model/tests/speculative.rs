//! The speculative next train: built on the unlanded one, promoted only in tested fast-forward when
//! main's tip is exactly its predecessor.
mod support;
use pg_train_model::*;
use support::*;

/// Train 0 is under test and two more parts are ready.
fn pipeline(policy: Policy) -> Model {
    let mut m = Model::new(policy, disjoint(5));
    register_all(&mut m, 0..2);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    register_all(&mut m, 2..4);
    m
}

fn tested_ff() -> Policy {
    Policy::every_ready_rides(Order::Oldest)
}

#[test]
fn a_candidate_is_built_on_the_unlanded_train_and_tested_meanwhile() {
    let mut m = pipeline(tested_ff());
    assert_eq!(denied(&mut m, Event::Select), Denied::OrdinaryOpen);
    assert_eq!(ok(&mut m, Event::Speculate { pred: 0 }), Outcome::Train(1));
    assert_eq!(
        (m.train_parts(1), m.train_ci(1)),
        (&[2, 3][..], Ci::Running)
    );
    // One candidate at a time, and parts in no other train.
    assert_eq!(
        denied(&mut m, Event::Speculate { pred: 0 }),
        Denied::OrdinaryOpen
    );
    assert_eq!(m.train_parts(0), [0, 1]);
}

#[test]
fn a_candidate_lands_only_in_tested_ff_on_its_exact_predecessor() {
    let mut m = pipeline(tested_ff());
    ok(&mut m, Event::Speculate { pred: 0 });
    ci(&mut m, 1, true);
    // The predecessor has not landed: main's tip is not the commit the candidate sits on.
    assert_eq!(denied(&mut m, Event::Promote { train: 1 }), Denied::Phase);
    assert_eq!(denied(&mut m, Event::Settle { train: 1 }), Denied::Phase);
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
    ok(&mut m, Event::Promote { train: 1 });
    assert_eq!((m.landed(), m.main()), (4, 2));
}

#[test]
fn a_candidate_whose_predecessor_is_no_longer_mains_tip_is_discarded() {
    for landed_first in [false, true] {
        let mut m = pipeline(tested_ff());
        ok(&mut m, Event::Speculate { pred: 0 });
        if landed_first {
            ci(&mut m, 0, true);
            ok(&mut m, Event::Settle { train: 0 });
        }
        ok(&mut m, Event::MainMove);
        assert!(m.train_closed(1));
        assert_eq!((m.status(2), m.status(3)), (Status::Ready, Status::Ready));
        assert_eq!(denied(&mut m, Event::Promote { train: 1 }), Denied::Phase);
    }
}

#[test]
fn any_change_to_the_predecessor_discards_the_candidate() {
    let changes = [
        Event::Amend { train: 0 },
        Event::Rebuild { train: 0 },
        Event::CiRedFix { train: 0, run: 1 },
        Event::CiResult {
            train: 0,
            run: 1,
            green: false,
        },
        Event::CiRed {
            train: 0,
            run: 1,
            culprit: 0,
        },
    ];
    for change in changes {
        let mut m = pipeline(tested_ff());
        ok(&mut m, Event::Speculate { pred: 0 });
        ok(&mut m, change.clone());
        assert!(m.train_closed(1), "{change:?} left the candidate alive");
        assert_eq!(m.status(2), Status::Ready);
    }
}

#[test]
fn a_promotion_in_a_pull_request_merge_mode_is_refused_and_the_candidate_stays_shadow() {
    let mut p = tested_ff();
    p.landing = Landing::PrMerge;
    let mut m = pipeline(p);
    ok(&mut m, Event::Speculate { pred: 0 });
    ci(&mut m, 1, true);
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(denied(&mut m, Event::Promote { train: 1 }), Denied::Mode);
    assert_eq!((m.landed(), m.main()), (2, 1));
}

#[test]
fn a_red_candidate_is_discarded_and_changes_nothing_on_main() {
    let mut m = pipeline(tested_ff());
    ok(&mut m, Event::Speculate { pred: 0 });
    assert_eq!(ci(&mut m, 1, false), Outcome::Discarded);
    assert!(m.pending_groups().is_empty());
    assert_eq!(
        (m.status(2), m.status(3), m.main()),
        (Status::Ready, Status::Ready, 0)
    );
}

#[test]
fn a_candidate_that_was_amended_after_its_ci_cannot_be_promoted() {
    let mut m = pipeline(tested_ff());
    ok(&mut m, Event::Speculate { pred: 0 });
    ci(&mut m, 1, true);
    ok(&mut m, Event::Amend { train: 1 });
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(
        denied(&mut m, Event::Promote { train: 1 }),
        Denied::TreeChanged
    );
}

#[test]
fn retry_precedence_holds_over_a_candidate() {
    let mut m = pipeline(tested_ff());
    ci(&mut m, 0, false);
    assert_eq!(denied(&mut m, Event::Speculate { pred: 0 }), Denied::Phase);
    ok(&mut m, Event::StartRetry);
    assert_eq!(denied(&mut m, Event::Speculate { pred: 1 }), Denied::Phase);
}

#[test]
fn a_candidate_is_never_rebuilt_in_place() {
    let mut m = pipeline(tested_ff());
    ok(&mut m, Event::Speculate { pred: 0 });
    assert_eq!(denied(&mut m, Event::Rebuild { train: 1 }), Denied::Phase);
}

#[test]
fn a_candidate_whose_verification_was_lost_cannot_be_promoted() {
    let mut m = Model::new(policy(1, 5, 0, Order::Oldest), disjoint(4));
    register_all(&mut m, 0..4);
    ok(&mut m, Event::Select);
    start(&mut m, 0);
    // The candidate takes part 1 and records parts 2 and 3 as left out at the target.
    assert_eq!(ok(&mut m, Event::Speculate { pred: 0 }), Outcome::Train(1));
    assert_eq!(m.left_out(1).len(), 2);
    ci(&mut m, 1, true);
    ok(&mut m, Event::OmitLeftOut { train: 1, part: 2 });
    ci(&mut m, 0, true);
    ok(&mut m, Event::Settle { train: 0 });
    assert_eq!(
        denied(&mut m, Event::Promote { train: 1 }),
        Denied::Unverified
    );
    assert!(m.invariants_hold());
}
