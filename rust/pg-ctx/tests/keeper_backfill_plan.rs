use pg_ctx::keeper_backfill_plan::build;
use serde_json::{json, Value};

fn inputs() -> (Vec<Value>, Vec<Vec<Value>>) {
    let reasons = [
        "no record [seat=review]",
        "unknown writer",
        "no commit evidence",
        "pinned seat",
        "item key mismatch",
        "PR number mismatch",
        "head mismatch: ambiguous verified commits at same epoch",
        "served_model unwitnessed",
    ];
    let items = (0..8)
        .map(|n| json!({"key":format!("plangraph:K-{n}"),"state":"in_review"}))
        .collect();
    let rows = reasons.iter().enumerate().map(|(n,reason)| json!({"key":format!("plangraph:K-{n}"),"status":"MISSING","reasons":[reason]})).collect();
    (items, vec![rows])
}

#[test]
fn keeper_backfill_plan_every_key_has_one_primary_class() {
    let (items, reports) = inputs();
    let plan = build(&items, &reports, 8).unwrap();
    assert_eq!(plan.total, 8);
    assert_eq!(plan.table.len(), 8);
    assert_eq!(
        plan.table
            .iter()
            .map(|r| r.primary_keys.len())
            .sum::<usize>(),
        8
    );
    assert_eq!(plan.followups.len(), 8);
    assert!(plan
        .table
        .iter()
        .all(|r| !r.rules.is_empty() && !r.owner.is_empty()));
    assert!(plan
        .followups
        .iter()
        .all(|r| r["depends_on"] == json!(["plangraph:INREVIEW-BACKFILL-CLASS-PLAN"])));
    assert_eq!(plan.status, "PROPOSED_UNRULED");
}

#[test]
fn keeper_backfill_plan_specific_evidence_beats_missing_other_source() {
    let (items, mut reports) = inputs();
    reports.push(vec![
        json!({"key":"plangraph:K-4","status":"MISSING","reasons":["no record"]}),
    ]);
    let plan = build(&items, &reports, 8).unwrap();
    let identity = plan
        .table
        .iter()
        .find(|r| r.class == "item key mismatch")
        .unwrap();
    assert_eq!(identity.primary_keys, vec!["plangraph:K-4"]);
    let missing = plan.table.iter().find(|r| r.class == "no record").unwrap();
    assert!(missing.secondary_keys.contains(&"plangraph:K-4".into()));
}

#[test]
fn keeper_backfill_plan_refuses_incomplete_or_unknown_inputs() {
    let (items, mut reports) = inputs();
    assert!(build(&items, &reports, 210).is_err());
    reports[0].pop();
    assert!(build(&items, &reports, 8).is_err());
    reports[0].push(json!({"key":"plangraph:K-7","status":"MISSING","reasons":["new refusal"]}));
    assert!(build(&items, &reports, 8).is_err());
}

#[test]
fn keeper_backfill_plan_cli_emits_table_and_gated_definitions() {
    let (items, reports) = inputs();
    let temp = tempfile::tempdir().unwrap();
    let snapshot = temp.path().join("snapshot.json");
    let report = temp.path().join("report.json");
    std::fs::write(
        &snapshot,
        serde_json::to_vec(&json!({"items":items})).unwrap(),
    )
    .unwrap();
    std::fs::write(&report, serde_json::to_vec(&reports[0]).unwrap()).unwrap();
    let mut args = vec![
        snapshot.to_string_lossy().into_owned(),
        report.to_string_lossy().into_owned(),
        "--expected".into(),
        "8".into(),
    ];
    let output: Value =
        serde_json::from_str(&pg_ctx::backfill_cli::execute(&args).unwrap()).unwrap();
    assert_eq!(output["total"], 8);
    assert_eq!(output["followups"].as_array().unwrap().len(), 8);
    args.push("--markdown".into());
    let table = pg_ctx::backfill_cli::execute(&args).unwrap();
    assert_eq!(table.lines().filter(|l| l.starts_with("| ")).count(), 9);
    assert!(table.contains("PROPOSED_UNRULED"));
}
