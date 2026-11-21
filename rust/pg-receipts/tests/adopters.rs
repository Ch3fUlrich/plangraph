//! The adopter scenarios below exercise the real keeper and durable queue.
//! These references complement the pure transition and interleaving suites.
//!
//! | Review scenario | Production-path test |
//! | --- | --- |
//! | R1 revoked credential reads only its own receipt | `tests/test_keeper_outbox.py::test_http_receipt_readback_after_token_revocation`, `tests/test_keeper_run_tokens_doors.py::test_reviewer_run_token_cannot_cross_seats_and_replays_exact_receipt` |
//! | R1 crash before send and append during acknowledgement | `keeper_outbox::tests::append_during_acknowledgement_survives_reopening` |
//! | R1 legacy outcome authorizes readback only | `tests/test_keeper_schema_upgrade.py::test_mixed_version_legacy_and_fresh_receipt_replay`, `tests/test_keeper_outbox.py::test_unproven_legacy_entries_remain_unacknowledged` |
//! | R1 delayed seat cannot follow a same-head resubmission | `tests/test_keeper_run_tokens_doors.py::test_seat_binding_refuses_late_same_head_on_direct_and_replay` |
//! | R2 heartbeat identity and submit round are independent | `tests/test_keeper_outbox.py::test_model_vectors_run_against_keeper` runs `keeper-adoption.tsv` |
//! | R2 legacy import preserves order and blocks unproven entries | `keeper_outbox::tests::legacy_import_keeps_ids_and_order_without_replaying_unproven_entries` |
//! | R2 seat payload binds once after admission | `tests/test_keeper_run_tokens_doors.py::test_seat_payload_immutable_and_unbound_ingresses_refused` |
//! | R3 local mutation and receipt commit together | `tests/test_keeper_outbox.py::test_crash_recording_receipt_rolls_back_mutation`, `tests/test_keeper_outbox.py::test_transient_refusal_does_not_become_a_receipt` |
//! | R3 submit waits for evidence disposition and can follow a verified retry | `keeper_outbox::tests::live_submit_cannot_pass_unverified_evidence`, `keeper_outbox::tests::verified_retry_unblocks_submit_for_its_claim` |
//! | R3 ingress requires the source round and seat binding | `tests/test_keeper_run_tokens_doors.py::test_old_round_submit_is_refused_on_live_and_receipt_routes`, `tests/test_keeper_run_tokens_doors.py::test_seat_payload_immutable_and_unbound_ingresses_refused` |

use pg_receipts::*;

#[test]
fn r2_seat_binds_choice_once_when_available() {
    let mut ledger = Ledger::default();
    let spec = Spec {
        binding: Some(42),
        ..Spec::new(Class::Gated, 1)
    };
    assert_eq!(ledger.begin(1, spec, &[]), Ok(State::Pending));
    assert!(ledger.apply(1, Stamp(0, 0), Step::Dispatch).is_err());
    assert_eq!(
        ledger.apply(1, Stamp(0, 0), Step::Bind(7)),
        Ok(Reply::State(State::Pending))
    );
    assert!(ledger.apply(1, Stamp(0, 0), Step::Bind(9)).is_err());
    assert!(matches!(
        ledger.apply(1, Stamp(0, 0), Step::Dispatch),
        Ok(Reply::Send(_))
    ));
}

#[test]
fn r1_settled_receipt_remains_readable_by_its_credential() {
    let mut ledger = Ledger::default();
    let spec = Spec {
        credential: 7,
        ..Spec::new(Class::Gated, 1)
    };
    ledger.begin(1, spec, &[]).unwrap();
    ledger.apply(1, Stamp(0, 0), Step::Bind(11)).unwrap();
    let Reply::Send(ticket) = ledger.apply(1, Stamp(0, 0), Step::Dispatch).unwrap() else {
        panic!("write was not dispatched");
    };
    ledger.admit(ticket).unwrap();
    ledger.effect(ticket).unwrap();
    ledger.apply(1, Stamp(0, 1), Step::Finish(true)).unwrap();
    ledger.takeover(0, true).unwrap();
    assert_eq!(ledger.read(1, 7, None), Some(State::Ok));
    assert_eq!(ledger.read(1, 8, None), None);
    assert_eq!(ledger.read(2, 7, None), None);
}
