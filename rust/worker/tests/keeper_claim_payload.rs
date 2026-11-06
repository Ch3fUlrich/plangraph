//! Exercise the actual Python claim builder, not a hand-maintained wire fixture.
use pg_worker::keeper::Claim;
use std::process::Command;

#[test]
fn keeper_payload_round_trips_with_and_without_check_metadata() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let script = r#"
import json
from plangraph.planner.worker_claim import build_claim
def card():
    return dict(check_cmd='false', write_set=['src/a.py'], outcome='implement')
claims = [build_claim('demo:C', card(), repo='repo', base_sha='abc123')]
explicit = dict(card(), check_kind='regression', check_reads=['fixtures/input.json'],
            smoke_checks=['true'], exports=['api'], heldout_check='test -f heldout',
            risk_tags=['high'])
claims.append(build_claim('demo:C', explicit, repo='repo', base_sha='abc123'))
print(json.dumps(claims))
"#;
    // Worktrees can share the repository's provisioned test environment via an
    // explicit path; never fall back to an ambient PATH interpreter.
    let python = std::env::var_os("PLANGRAPH_TEST_PYTHON")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| root.join(".venv/bin/python3"));
    let output = Command::new(&python)
        .args(["-E", "-s", "-c", script])
        .current_dir(root.join("src"))
        .output()
        .expect("repository test Python required: provision .venv or set PLANGRAPH_TEST_PYTHON");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let payloads: Vec<serde_json::Value> = serde_json::from_slice(&output.stdout).unwrap();
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
    for (key, value) in serialized.as_object().unwrap() {
        assert_eq!(Some(value), payload.get(key), "claim field {key}");
    }
}

#[test]
fn readiness_only_builder_keys_do_not_change_claim_contract() {
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
