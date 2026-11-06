//! Exercise the actual Python claim builder, not a hand-maintained wire fixture.
use pg_worker::keeper::Claim;
use std::process::Command;

#[test]
fn keeper_payload_round_trips_with_and_without_check_metadata() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let script = r#"
import json
from plangraph.planner.worker_claim import build_claim
card = dict(check_cmd='false', write_set=['src/a.py'], outcome='implement')
claims = [build_claim('demo:C', card, repo='repo', base_sha='abc123')]
card.update(check_kind='regression', check_reads=['fixtures/input.json'],
            smoke_checks=['true'], exports=['api'], heldout_check='test -f heldout',
            risk_tags=['high'])
claims.append(build_claim('demo:C', card, repo='repo', base_sha='abc123'))
print(json.dumps(claims))
"#;
    let output = Command::new("python3")
        .args([
            "-I",
            "-c",
            &format!(
                "import sys; sys.path.insert(0, {:?});\n{}",
                root.join("src").to_str().unwrap(),
                script
            ),
        ])
        .output()
        .expect("python3 is required for the keeper contract test");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let payloads: Vec<serde_json::Value> = serde_json::from_slice(&output.stdout).unwrap();
    for (index, payload) in payloads.into_iter().enumerate() {
        let claim: Claim = serde_json::from_value(payload.clone()).unwrap();
        assert_eq!(serde_json::to_value(&claim).unwrap(), payload);
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
