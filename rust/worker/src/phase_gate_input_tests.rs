use super::*;

#[test]
fn claim_gate_metadata_reaches_card_input() {
    let legacy = serde_json::from_value(serde_json::json!({
        "claim": {"card":"a", "repo":"r", "base_sha":"abc",
            "check_cmd":"check", "allow":[], "forbid":[], "prompt":"fix",
            "risk_tagged":true, "heldout_cmd":"heldout",
            "smoke_checks":["smoke"], "exports":["api"]},
        "legs":[], "workroot":"work", "main_branch":"main"
    }))
    .unwrap();
    let input = with_gate_inputs(legacy);
    assert!(input.risk_tagged);
    assert_eq!(input.heldout_cmd, "heldout");
    assert_eq!(input.smoke_checks, vec!["smoke"]);
    assert_eq!(input.exports, vec!["api"]);
    assert_eq!(input.base_sha.as_deref(), Some("abc"));
}
