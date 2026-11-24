//! Each new disposition has one case the verifier accepts and one it refuses.
mod support;
use pg_train_model::*;
use support::*;

fn open_train(specs: Vec<PartSpec>, target: u8, ready: &[PartId]) -> Model {
    let mut m = Model::new(policy(target, 2, 1, Order::Oldest), specs);
    register_all(&mut m, ready.iter().copied());
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    m
}

fn verify(m: &mut Model, part: PartId) -> Result<Outcome, Denied> {
    m.apply(Event::VerifyLeftOut { train: 0, part })
}

fn record(m: &mut Model, part: PartId, disposition: Disposition) {
    ok(
        m,
        Event::RecordLeftOut {
            train: 0,
            part,
            disposition,
        },
    );
}

#[test]
fn overlap_is_accepted_for_a_real_overlap_inside_the_bound_and_refused_otherwise() {
    let specs = vec![spec(0b1, None), spec(0b1, None), spec(0b10, None)];
    let mut m = open_train(specs, 4, &[0, 1, 2]);
    assert_eq!(
        m.left_out(0),
        [(1, Disposition::Overlap { with: 0, waited: 1 })]
    );
    assert_eq!(verify(&mut m, 1), Ok(Outcome::Accepted));
    // Refused: the named part does not overlap; or the part had used up its bound.
    record(&mut m, 1, Disposition::Overlap { with: 2, waited: 1 });
    assert_eq!(verify(&mut m, 1), Err(Denied::Disposition));
    record(&mut m, 1, Disposition::Overlap { with: 0, waited: 2 });
    assert_eq!(verify(&mut m, 1), Err(Denied::Disposition));
    record(&mut m, 1, Disposition::Overlap { with: 1, waited: 0 });
    assert_eq!(verify(&mut m, 1), Err(Denied::Disposition));
}

#[test]
fn parent_not_landed_is_accepted_while_the_parent_has_no_verified_landing_and_refused_after() {
    let specs = vec![spec(1, None), spec(2, Some(0)), spec(4, None)];
    let mut m = open_train(specs, 4, &[0, 2]);
    // Part 1 registers after the train was cut: it was left out by nothing, so record it.
    register_all(&mut m, [1]);
    record(&mut m, 1, Disposition::ParentNotLanded { parent: 0 });
    // The parent rides in this open train, so "not landed" does not justify leaving the child out.
    assert_eq!(verify(&mut m, 1), Err(Denied::Disposition));
    // A parent that is not in the train and has no verified landing does.
    let specs = vec![spec(1, None), spec(2, Some(0)), spec(4, None)];
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), specs);
    register_all(&mut m, [0, 1, 2]);
    ok(&mut m, Event::Drop { part: 0 });
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    assert_eq!(
        m.left_out(0),
        [(1, Disposition::ParentNotLanded { parent: 0 })]
    );
    assert_eq!(verify(&mut m, 1), Ok(Outcome::Accepted));
    // Wrong parent named.
    record(&mut m, 1, Disposition::ParentNotLanded { parent: 2 });
    assert_eq!(verify(&mut m, 1), Err(Denied::Disposition));
}

#[test]
fn target_is_accepted_at_the_size_target_and_refused_below_it() {
    let specs = vec![spec(1, None), spec(2, None), spec(4, None)];
    let mut m = open_train(specs, 2, &[0, 1, 2]);
    assert_eq!(m.left_out(0), [(2, Disposition::Target)]);
    assert_eq!(verify(&mut m, 2), Ok(Outcome::Accepted));
    ok(&mut m, Event::Drop { part: 1 });
    assert_eq!(m.train_parts(0), [0]);
    assert_eq!(verify(&mut m, 2), Err(Denied::Disposition));
}

#[test]
fn a_settled_legacy_train_has_no_included_list_and_the_child_waits_for_the_verified_outcome() {
    let specs = vec![spec(1, None), spec(2, Some(0)), spec(4, None)];
    let mut m = Model::new(policy(2, 2, 1, Order::Oldest), specs);
    register_all(&mut m, [0]);
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
    ok(&mut m, Event::SettleLegacy { train: 0 });
    assert_eq!(m.status(0), Status::Landed);
    register_all(&mut m, [1, 2]);
    ok(&mut m, Event::Tick);
    // The child cannot ride on a landing that nothing verified; the independent part does.
    ok(&mut m, Event::Select);
    assert_eq!(m.train_parts(1), [2]);
    assert_eq!(
        m.left_out(1),
        [(1, Disposition::ParentNotLanded { parent: 0 })]
    );
    assert_eq!(
        m.apply(Event::VerifyLeftOut { train: 1, part: 1 }),
        Ok(Outcome::Accepted)
    );
    // Only the verified outcome of the parent's train releases the child, and only once.
    ok(&mut m, Event::VerifyOutcome { train: 0 });
    assert_eq!(
        m.apply(Event::VerifyLeftOut { train: 1, part: 1 }),
        Err(Denied::Disposition)
    );
    assert_eq!(
        denied(&mut m, Event::VerifyOutcome { train: 1 }),
        Denied::Phase
    );
    start(&mut m, 1);
    ok(
        &mut m,
        Event::CiResult {
            train: 1,
            green: true,
        },
    );
    ok(&mut m, Event::Settle { train: 1 });
    ok(&mut m, Event::Select);
    assert_eq!(m.train_parts(2), [1]);
}

#[test]
fn a_train_with_an_included_list_verifies_at_settlement() {
    let specs = vec![spec(1, None), spec(2, Some(0))];
    let mut m = Model::new(policy(1, 2, 1, Order::Oldest), specs);
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
    ok(&mut m, Event::Select);
    assert_eq!(m.train_parts(1), [1]);
}

#[test]
fn conflict_and_descendant_are_accepted_for_a_real_conflict_and_refused_otherwise() {
    let specs = vec![spec(1, None), spec(2, Some(0)), spec(4, None)];
    let mut m = Model::new(policy(4, 2, 1, Order::Oldest), specs);
    register_all(&mut m, [0, 1, 2]);
    ok(&mut m, Event::Tick);
    ok(&mut m, Event::Select);
    ok(&mut m, Event::Conflict { train: 0, part: 0 });
    assert_eq!(
        m.left_out(0),
        [
            (0, Disposition::Conflict),
            (1, Disposition::Descendant { of: 0 })
        ]
    );
    assert_eq!(verify(&mut m, 0), Ok(Outcome::Accepted));
    assert_eq!(verify(&mut m, 1), Ok(Outcome::Accepted));
    // Refused: a conflict nobody found, and a descendant of a part that rides.
    record(&mut m, 1, Disposition::Conflict);
    assert_eq!(verify(&mut m, 1), Err(Denied::Disposition));
    record(&mut m, 1, Disposition::Descendant { of: 2 });
    assert_eq!(verify(&mut m, 1), Err(Denied::Disposition));
}
