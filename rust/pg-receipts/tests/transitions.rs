use pg_receipts::*;

fn start(class: Class) -> Ledger {
    let mut ledger = Ledger::default();
    ledger.begin(1, Spec::new(class, 7), &[]).unwrap();
    ledger.apply(1, Stamp(0, 0), Step::Bind(42)).unwrap();
    ledger
}

fn dispatch(ledger: &mut Ledger, stamp: Stamp) -> Ticket {
    let Reply::Send(ticket) = ledger.apply(1, stamp, Step::Dispatch).unwrap() else {
        panic!("dispatch did not grant permission");
    };
    ticket
}

#[test]
fn r1_concurrent_dispatch_and_delayed_reply() {
    let mut ledger = start(Class::Gated);
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    assert!(ledger.apply(1, Stamp(0, 0), Step::Dispatch).is_err());
    ledger.admit(ticket).unwrap();
    assert!(ledger.admit(ticket).is_err());
}

#[test]
fn r2_validation_pauses_across_deadline() {
    let mut ledger = start(Class::Gated);
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.tick();
    assert!(ledger.admit(ticket).is_err());
    ledger.apply(1, Stamp(0, 1), Step::Expire).unwrap();
    assert!(ledger.admit(ticket).is_err());
    assert_eq!(ledger.state(1), Some(State::Pending));
}

#[test]
fn r3_foreign_expiry_preserves_uncertainty() {
    let mut ledger = start(Class::Foreign);
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.tick();
    ledger.apply(1, Stamp(0, 1), Step::Expire).unwrap();
    ledger.takeover(0, true).unwrap();
    assert_eq!(ledger.state(1), Some(State::Unknown));
    assert!(ledger.apply(1, Stamp(1, 1), Step::Dispatch).is_err());
    assert!(ledger
        .apply(1, Stamp(1, 1), Step::Observe(Proof::AbsentNow))
        .is_err());
    ledger.deliver(ticket).unwrap();
    ledger
        .apply(1, Stamp(1, 1), Step::Observe(Proof::Present))
        .unwrap();
    assert_eq!(ledger.state(1), Some(State::Ok));
}

#[test]
fn r3_resolve_does_not_release_conflict() {
    let mut ledger = start(Class::Foreign);
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.tick();
    ledger.apply(1, Stamp(0, 1), Step::Expire).unwrap();
    ledger.apply(1, Stamp(0, 1), Step::Resolve(3)).unwrap();
    assert!(ledger.begin(2, Spec::new(Class::Foreign, 7), &[]).is_err());
    ledger.deliver(ticket).unwrap();
    ledger
        .apply(1, Stamp(0, 1), Step::Observe(Proof::Present))
        .unwrap();
    assert!(ledger.begin(2, Spec::new(Class::Foreign, 7), &[]).is_ok());
}

#[test]
fn r3_stale_settlement_and_terminal_result_immutability() {
    let mut ledger = start(Class::Gated);
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.admit(ticket).unwrap();
    ledger.effect(ticket).unwrap();
    ledger.takeover(0, true).unwrap();
    assert!(ledger.apply(1, Stamp(0, 1), Step::Finish(true)).is_err());
    ledger
        .apply(1, Stamp(1, 1), Step::Observe(Proof::Present))
        .unwrap();
    assert!(ledger.apply(1, Stamp(1, 1), Step::Finish(false)).is_err());
    assert_eq!(ledger.state(1), Some(State::Ok));
}

#[test]
fn r1_protected_writes_need_destination_authority() {
    let mut ledger = start(Class::Protected {
        authority_at_destination: false,
    });
    assert!(ledger.apply(1, Stamp(0, 0), Step::Dispatch).is_err());
}

#[test]
fn r1_parent_manifest_survives_crash() {
    let mut ledger = Ledger::default();
    let parent = Spec::new(Class::Gated, 7);
    let child = Spec::new(Class::Gated, 8);
    ledger.begin(1, parent, &[(2, child)]).unwrap();
    assert!(ledger.begin(1, parent, &[]).is_err());
    assert!(ledger.apply(2, Stamp(0, 0), Step::Activate).is_err());
    ledger.apply(1, Stamp(0, 0), Step::Bind(9)).unwrap();
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.admit(ticket).unwrap();
    ledger.effect(ticket).unwrap();
    ledger.apply(1, Stamp(0, 1), Step::Finish(true)).unwrap();
    ledger.takeover(0, true).unwrap();
    assert_eq!(ledger.obligations(), vec![2]);
    ledger.apply(2, Stamp(1, 0), Step::Activate).unwrap();
}

#[test]
fn r1_alternate_landing_ingress_needs_the_same_authority() {
    let mut ledger = start(Class::Protected {
        authority_at_destination: true,
    });
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    assert!(ledger.deliver(ticket).is_err());
    assert!(ledger.effect(ticket).is_err());
    ledger.admit(ticket).unwrap();
    ledger.effect(ticket).unwrap();
}

#[test]
fn r1_launch_intent_precedes_send_and_survives_replacement() {
    let mut ledger = start(Class::Gated);
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    assert!(ledger.begin(2, Spec::new(Class::Gated, 7), &[]).is_err());
    assert!(ledger.takeover(0, true).is_err());
    ledger.admit(ticket).unwrap();
    ledger.takeover(0, true).unwrap();
    assert_eq!(ledger.state(1), Some(State::Admitted));
    assert!(ledger
        .apply(1, Stamp(1, 1), Step::Observe(Proof::FencedAbsent))
        .is_err());
}

#[test]
fn r2_timed_out_protected_write_keeps_authority_until_settled() {
    let mut ledger = start(Class::Protected {
        authority_at_destination: true,
    });
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.admit(ticket).unwrap();
    ledger.tick();
    assert!(ledger.apply(1, Stamp(0, 1), Step::Expire).is_err());
    assert!(ledger.takeover(0, true).is_err());
    ledger.effect(ticket).unwrap();
    ledger.apply(1, Stamp(0, 1), Step::Finish(true)).unwrap();
    ledger.takeover(0, true).unwrap();
}

#[test]
fn r2_late_patch_cannot_be_overtaken() {
    let mut ledger = start(Class::Foreign);
    let old = dispatch(&mut ledger, Stamp(0, 0));
    ledger
        .apply(1, Stamp(0, 1), Step::Observe(Proof::Undecidable))
        .unwrap();
    assert!(ledger.begin(2, Spec::new(Class::Foreign, 7), &[]).is_err());
    assert!(ledger.apply(1, Stamp(0, 1), Step::Dispatch).is_err());
    ledger.deliver(old).unwrap();
    ledger
        .apply(1, Stamp(0, 1), Step::Observe(Proof::Present))
        .unwrap();
    ledger.begin(2, Spec::new(Class::Foreign, 7), &[]).unwrap();
}

#[test]
fn r2_failed_required_child_remains_owed() {
    let mut ledger = failed_child(false);
    assert_eq!(ledger.obligations(), vec![2]);
    assert!(ledger.apply(2, Stamp(1, 1), Step::RetryChild(3)).is_err());
    ledger.apply(2, Stamp(1, 1), Step::Resolve(1)).unwrap();
    assert!(ledger.obligations().is_empty());
}

#[test]
fn r3_due_children_activate_and_retry_under_successor() {
    let mut ledger = failed_child(true);
    assert!(ledger.apply(2, Stamp(0, 1), Step::RetryChild(3)).is_err());
    ledger.apply(2, Stamp(1, 1), Step::RetryChild(3)).unwrap();
    assert_eq!(ledger.state(2), Some(State::Failed));
    assert_eq!(ledger.obligations(), vec![3]);
    assert!(matches!(
        ledger.apply(3, Stamp(1, 0), Step::Dispatch),
        Ok(Reply::Send(_))
    ));
}

fn failed_child(retryable: bool) -> Ledger {
    let mut ledger = Ledger::default();
    let parent = Spec::new(Class::Gated, 7);
    let child = Spec {
        retryable,
        ..Spec::new(Class::Gated, 8)
    };
    ledger.begin(1, parent, &[(2, child)]).unwrap();
    ledger.apply(1, Stamp(0, 0), Step::Bind(1)).unwrap();
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.admit(ticket).unwrap();
    ledger.effect(ticket).unwrap();
    ledger.apply(1, Stamp(0, 1), Step::Finish(true)).unwrap();
    ledger.apply(2, Stamp(0, 0), Step::Activate).unwrap();
    ledger.apply(2, Stamp(0, 0), Step::Bind(2)).unwrap();
    let Reply::Send(ticket) = ledger.apply(2, Stamp(0, 0), Step::Dispatch).unwrap() else {
        panic!()
    };
    ledger.fence(ticket).unwrap();
    ledger.apply(2, Stamp(0, 1), Step::Finish(false)).unwrap();
    ledger.takeover(0, true).unwrap();
    ledger
}

#[test]
fn r2_cancel_intent_survives_takeover_before_reservation() {
    let mut ledger = start(Class::Launch);
    ledger
        .apply(1, Stamp(0, 0), Step::RequestCancel(2))
        .unwrap();
    ledger.takeover(0, true).unwrap();
    assert!(ledger.apply(1, Stamp(1, 0), Step::Dispatch).is_err());
    assert_eq!(ledger.obligations(), vec![1, 2]);
    ledger
        .apply(1, Stamp(1, 0), Step::Observe(Proof::FencedAbsent))
        .unwrap();
    assert_eq!(ledger.state(1), Some(State::Failed));
}

#[test]
fn r2_lost_replies_cannot_exceed_budget() {
    let mut ledger = Ledger::default();
    ledger
        .begin(
            1,
            Spec {
                resends: 1,
                ..Spec::new(Class::Budgeted, 7)
            },
            &[],
        )
        .unwrap();
    ledger.apply(1, Stamp(0, 0), Step::Bind(1)).unwrap();
    let first = dispatch(&mut ledger, Stamp(0, 0));
    ledger
        .apply(1, Stamp(0, 1), Step::Observe(Proof::AbsentNow))
        .unwrap();
    let second = dispatch(&mut ledger, Stamp(0, 1));
    ledger
        .apply(1, Stamp(0, 2), Step::Observe(Proof::AbsentNow))
        .unwrap();
    assert!(ledger.apply(1, Stamp(0, 2), Step::Dispatch).is_err());
    ledger.deliver(second).unwrap();
    ledger.deliver(first).unwrap();
    assert_eq!(ledger.effects(1), 2);
}

#[test]
fn r3_ci_rerun_budget_is_bound_to_original_workflow_attempt() {
    let mut ledger = Ledger::default();
    let spec = Spec {
        binding: Some(71),
        resends: 1,
        ..Spec::new(Class::Budgeted, 7)
    };
    ledger.begin(1, spec, &[]).unwrap();
    assert!(ledger
        .begin(
            2,
            Spec {
                conflict: None,
                ..spec
            },
            &[]
        )
        .is_err());
}

#[test]
fn r3_release_and_cancel_require_a_fenced_launch() {
    let mut ledger = start(Class::Launch);
    let launch = dispatch(&mut ledger, Stamp(0, 0));
    ledger.admit(launch).unwrap();
    ledger
        .apply(1, Stamp(0, 1), Step::RequestCancel(2))
        .unwrap();
    let Reply::Send(cancel) = ledger.apply(2, Stamp(0, 0), Step::Dispatch).unwrap() else {
        panic!()
    };
    ledger.admit(cancel).unwrap();
    ledger.effect(cancel).unwrap();
    assert!(ledger.apply(2, Stamp(0, 1), Step::Finish(true)).is_err());
    assert!(ledger.begin(3, Spec::new(Class::Gated, 7), &[]).is_err());
    ledger.fence(launch).unwrap();
    ledger
        .apply(1, Stamp(0, 1), Step::Observe(Proof::FencedAbsent))
        .unwrap();
    ledger.apply(2, Stamp(0, 1), Step::Finish(true)).unwrap();
    ledger.begin(3, Spec::new(Class::Gated, 7), &[]).unwrap();
}

#[test]
fn r3_cancel_after_process_creation_requires_confirmed_stop() {
    let mut ledger = start(Class::Launch);
    let launch = dispatch(&mut ledger, Stamp(0, 0));
    ledger.admit(launch).unwrap();
    ledger.effect(launch).unwrap();
    ledger.apply(1, Stamp(0, 1), Step::Finish(true)).unwrap();
    ledger
        .apply(1, Stamp(0, 1), Step::RequestCancel(2))
        .unwrap();
    let Reply::Send(cancel) = ledger.apply(2, Stamp(0, 0), Step::Dispatch).unwrap() else {
        panic!()
    };
    ledger.admit(cancel).unwrap();
    ledger.effect(cancel).unwrap();
    assert!(ledger.apply(2, Stamp(0, 1), Step::Finish(true)).is_err());
    ledger.confirm_stopped(launch).unwrap();
    ledger.apply(2, Stamp(0, 1), Step::Finish(true)).unwrap();
    assert_eq!(ledger.state(1), Some(State::Ok));
}

#[test]
fn children_of_failed_parent_cannot_apply_or_block_other_intents() {
    let mut ledger = Ledger::default();
    let parent = Spec::new(Class::Gated, 7);
    let child = Spec::new(Class::Gated, 8);
    ledger.begin(1, parent, &[(2, child)]).unwrap();
    ledger.apply(1, Stamp(0, 0), Step::Bind(1)).unwrap();
    let ticket = dispatch(&mut ledger, Stamp(0, 0));
    ledger.fence(ticket).unwrap();
    ledger.apply(1, Stamp(0, 1), Step::Finish(false)).unwrap();
    assert!(ledger.obligations().is_empty());
    assert!(ledger.apply(2, Stamp(0, 0), Step::Activate).is_err());
    assert!(ledger.begin(3, child, &[]).is_ok());
}
