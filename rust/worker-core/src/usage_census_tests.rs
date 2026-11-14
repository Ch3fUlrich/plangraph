use super::*;
use serde_json::json;

#[test]
fn census_recognizes_canonical_fleet_role_names() {
    assert_eq!(role("plangraph-replan-0042"), "replan");
    assert_eq!(role("build-L2-0042"), "L2");
    assert_eq!(role("build-L1-0042"), "L1-build");
    assert_eq!(role("L1-research-0042"), "L1-other");
}

#[test]
fn census_deduplicates_stream_blocks_and_keeps_subagent_identity() {
    let dir = tempfile::tempdir().unwrap();
    let project = dir.path().join("project");
    std::fs::create_dir_all(project.join("s/subagents")).unwrap();
    let turn = |id, output| {
        json!({"type":"assistant","timestamp":"2026-11-12T12:00:00Z",
        "message":{"id":id,"usage":{"input_tokens":20,"output_tokens":output,
        "cache_read_input_tokens":100}}})
    };
    let rows = [
        json!({"type":"custom-title","customTitle":"routing-42"}),
        json!({"type":"user","trigger":"beat"}),
        turn("a", 1),
        turn("a", 10),
    ];
    std::fs::write(
        project.join("s.jsonl"),
        rows.iter().map(|v| format!("{v}\n")).collect::<String>(),
    )
    .unwrap();
    std::fs::write(
        project.join("s/subagents/agent-a.jsonl"),
        format!(
            "{}\n{}\n",
            json!({"type":"system","spawn_kind":"fresh","trigger":"work"}),
            turn("a", 5)
        ),
    )
    .unwrap();
    let report = census(
        dir.path(),
        "2026-11-10T00:00:00Z",
        "2026-11-14T00:00:00Z",
        &[json!({"number":1,"mergedAt":"2026-11-12T13:00:00Z"})],
    )
    .unwrap();
    assert_eq!(report["roles"]["router"]["turns"], 1);
    assert_eq!(report["roles"]["router"]["weighted_tokens"], 40.0);
    assert_eq!(report["roles"]["router"]["trigger_share"]["beat"], 1.0);
    assert_eq!(report["roles"]["subagent-fresh"]["turns"], 1);
    assert_eq!(
        report["roles"]["subagent-fresh"]["weighted_tokens_per_merged_pr"],
        35.0
    );
    assert_eq!(report["throughput_guard"], "revert-cut");
}

#[test]
fn census_unknown_provenance_is_visible_and_invalid_usage_is_counted() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("p")).unwrap();
    std::fs::write(dir.path().join("p/s.jsonl"), concat!(
        "{\"type\":\"assistant\",\"timestamp\":\"2026-11-13T12:00:00Z\",\"message\":{\"id\":\"a\",\"usage\":{\"output_tokens\":-1}}}\n",
        "garbage\n")).unwrap();
    let report = census(
        dir.path(),
        "2026-11-10T00:00:00Z",
        "2026-11-14T00:00:00Z",
        &[],
    )
    .unwrap();
    assert_eq!(report["diagnostics"]["malformed_records"], 2);
    assert!(report["roles"]["unknown"]["weighted_tokens_per_merged_pr"].is_null());
    assert!(census(
        dir.path(),
        "2026-11-14T00:00:00Z",
        "2026-11-10T00:00:00Z",
        &[]
    )
    .is_err());
}
