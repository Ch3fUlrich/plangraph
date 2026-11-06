//! Shared wire fixture pinned to the real Python builder by test_worker_claim.py.
use pg_worker::keeper::Claim;

#[test]
fn keeper_payload_round_trips_with_and_without_check_metadata() {
    let payloads: Vec<serde_json::Value> =
        serde_json::from_str(include_str!("fixtures/keeper_claim_payload.json")).unwrap();
    for (index, payload) in payloads.into_iter().enumerate() {
        let claim: Claim = serde_json::from_value(payload.clone()).unwrap();
        assert_claim_projection(&claim, &payload);
        assert_eq!(claim.base_sha, "abc123");
        assert_eq!(
            claim.check_kind,
            if index == 0 { "change" } else { "regression" }
        );
        assert_eq!(claim.risk_tagged, index == 1);
        if index == 0 {
            assert!(claim.check_reads.is_empty());
            assert!(claim.heldout_cmd.is_empty());
            assert!(claim.smoke_checks.is_empty());
            assert!(claim.exports.is_empty());
        } else {
            assert_eq!(claim.check_reads, ["fixtures/input.json"]);
            assert!(claim.forbid.contains(&"fixtures/input.json".to_string()));
            assert_eq!(claim.heldout_cmd, "test -f heldout");
            assert_eq!(claim.smoke_checks, ["true"]);
            assert_eq!(claim.exports, ["api"]);
        }
    }
}

fn assert_claim_projection(claim: &Claim, payload: &serde_json::Value) {
    let serialized = serde_json::to_value(claim).unwrap();
    let payload_keys: std::collections::BTreeSet<_> = payload.as_object().unwrap().keys().collect();
    let claim_keys: std::collections::BTreeSet<_> =
        serialized.as_object().unwrap().keys().collect();
    assert_eq!(
        payload_keys, claim_keys,
        "unexpected builder fields (no readiness-only fields)"
    );
    for (key, value) in serialized.as_object().unwrap() {
        assert_eq!(Some(value), payload.get(key), "claim field {key}");
    }
}

#[test]
#[should_panic(expected = "unexpected builder fields")]
fn unexpected_builder_keys_fail_projection() {
    let mut payload = serde_json::json!({
        "card": "c", "repo": "r", "base_sha": "abc", "check_cmd": "false",
        "allow": [], "forbid": [], "prompt": "p", "check_kind": "change",
        "check_reads": [], "smoke_checks": [], "exports": [],
        "heldout_cmd": "", "risk_tagged": false
    });
    payload["readiness_only"] = serde_json::json!(true);
    let claim: Claim = serde_json::from_value(payload.clone()).unwrap();
    assert_claim_projection(&claim, &payload);
}

#[test]
fn legacy_payload_missing_all_check_metadata_uses_defaults() {
    let claim: Claim = serde_json::from_value(serde_json::json!({
        "card": "c", "repo": "r", "base_sha": "abc", "check_cmd": "false",
        "allow": [], "forbid": [], "prompt": "p"
    }))
    .unwrap();
    assert_eq!(claim.check_kind, "change");
    assert!(claim.check_reads.is_empty());
    assert!(claim.heldout_cmd.is_empty());
    assert!(claim.smoke_checks.is_empty());
    assert!(claim.exports.is_empty());
    assert!(!claim.risk_tagged);
}

#[test]
fn unknown_check_kinds_remain_readable_in_recorded_claims() {
    let payloads: Vec<serde_json::Value> =
        serde_json::from_str(include_str!("fixtures/keeper_claim_payload.json")).unwrap();
    for kind in ["typo", "unknown", "CHANGE"] {
        let mut payload = payloads[0].clone();
        payload["check_kind"] = kind.into();
        let claim: Claim = serde_json::from_value(payload).unwrap();
        assert_eq!(claim.check_kind, kind);
        assert_eq!(serde_json::to_value(claim).unwrap()["check_kind"], kind);
    }
}

#[test]
fn recorded_legacy_empty_check_kind_normalizes_to_change() {
    let payloads: Vec<serde_json::Value> =
        serde_json::from_str(include_str!("fixtures/keeper_claim_payload.json")).unwrap();
    let mut payload = payloads[0].clone();
    payload["check_kind"] = "".into();
    let claim: Claim = serde_json::from_value(payload).unwrap();
    assert_eq!(claim.check_kind, "change");
    assert_eq!(serde_json::to_value(claim).unwrap()["check_kind"], "change");
}

#[test]
fn constructed_default_matches_wire_default() {
    assert_eq!(Claim::default().check_kind, "change");
}

#[test]
fn non_string_check_kinds_are_not_legacy_strings() {
    let payloads: Vec<serde_json::Value> =
        serde_json::from_str(include_str!("fixtures/keeper_claim_payload.json")).unwrap();
    for kind in [
        serde_json::Value::Null,
        serde_json::json!(true),
        serde_json::json!(false),
        serde_json::json!(1),
        serde_json::json!([]),
        serde_json::json!({}),
    ] {
        let mut payload = payloads[0].clone();
        payload["check_kind"] = kind;
        assert!(serde_json::from_value::<Claim>(payload).is_err());
    }
}
