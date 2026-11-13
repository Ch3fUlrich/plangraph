use serde_json::{json, Value};
#[test]
fn historical_approval_parser_parity() {
    for raw in include_str!("fixtures/backfill_parse.jsonl").lines() {
        let v: Value = serde_json::from_str(raw).unwrap();
        assert_eq!(
            json!(pg_fleet::backfill::parse_row(v["row"].as_str().unwrap())),
            v["expected"],
            "{}",
            v["row"]
        );
    }
}
#[test]
fn original_report_snapshots() {
    for raw in include_str!("fixtures/backfill_report.jsonl").lines() {
        let v: Value = serde_json::from_str(raw).unwrap();
        let merged = pg_fleet::backfill::merges_from_json(&v["merged"]);
        let decisions = v["decisions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect::<Vec<_>>()
            .join("\n");
        let r = pg_fleet::backfill::report(v["items"].as_array().unwrap(), &merged, &decisions);
        assert_eq!(json!(r.rows), v["expected"], "{decisions}");
        assert_eq!(json!(r.missing), v["missing"], "{decisions}");
    }
}
