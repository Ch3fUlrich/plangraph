use plangraph_cli::feeder::*;
use serde_json::json;

fn winner(v: &serde_json::Value, runs: &[Run]) -> Option<String> {
    pick(v, runs, &Policy::default()).map(|t| t.key)
}

#[test]
fn ages_compare_actual_instants_and_unknown_ages_sort_last() {
    let v = json!({"items":[
        {"key":"p:A","priority":1,"created_at":"2026-01-01T00:00:00Z"},
        {"key":"p:B","priority":1,"created_at":"2026-01-01T00:30:00+01:00"},
        {"key":"p:C","priority":1,"created_at":"invalid"}
    ]});
    assert_eq!(winner(&v, &[]).unwrap(), "p:B");
}
