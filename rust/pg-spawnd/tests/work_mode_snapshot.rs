//! Exercise the real CLI serialization boundary consumed by the Rust resolver.
use serde_json::Value;
use std::process::Command;

#[test]
fn writer_snapshot_preserves_catalog_score_and_planner_quota_filter() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    // Fixture plans represent an exhausted strong leg and one admitted weak leg.
    // The policy itself is tested in worker-core; this tests adapter data loss.
    let script = r#"
from types import SimpleNamespace as N
from unittest.mock import patch
from plangraph.spawner import cli
strong = N(id='strong', model='strong-model', harness='opencode', roles=['implement'])
weak = N(id='weak', model='weak-model', harness='opencode', roles=['implement'])
writer = N(to_dict=lambda: dict(legs=[dict(id=weak.id)], skipped=[dict(leg=strong.id, reason='limited')]))
service = N(route=lambda *a, **kw: writer, _route=lambda *a, **kw: writer)
rows = [dict(id=strong.model, aa_intelligence=60), dict(id=weak.model, aa_intelligence=40)]
with patch.object(cli, 'SpawnService', return_value=service), patch('plangraph.planner.legs.DEFAULT_LEGS', [strong, weak]), patch('plangraph.routing_support.resolver.get_catalog_models', return_value=rows):
    cli.main(['route', '--role', 'review', '--exclude-family', 'openai', '--card', '{"estimate_tokens":100}', '--writer-snapshot', '--json'])
"#;
    let output = Command::new("python3")
        .args(["-c", script])
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env(
            "PLANGRAPH_CATALOG_DB",
            std::env::temp_dir()
                .join(format!("pg-no-catalog-{}", std::process::id()))
                .join("catalog.db"),
        )
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let row: Value = serde_json::from_slice(&output.stdout).unwrap();
    let legs = &row["work_mode_writer_legs"];
    assert_eq!(legs[0]["aa_intelligence"], 60);
    assert_eq!(legs[0]["served"], false);
    assert_eq!(legs[0]["unavailable_reason"], "limited");
    assert_eq!(legs[1]["aa_intelligence"], 40);
    assert_eq!(legs[1]["served"], true);
    assert!(legs[1]["unavailable_reason"].is_null());
}
