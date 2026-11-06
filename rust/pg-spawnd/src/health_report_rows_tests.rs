use super::*;
use serde_json::json;
#[test]
fn legacy_known_facts() {
    let rows = corrected(
        &[
            json!({"run_id":"legacy","role":"research","attempts":[{"exit_code":1},{"exit_code":0,"content":true,"tool":false,"tree_changed":false,"verdict_usable":false,"tool_call_only":false},{}]}),
        ],
        &[],
    );
    assert_eq!(rows[0]["outcome"], "CRASH");
    assert_eq!(rows[1]["outcome"], "OK");
    assert!(rows[2]["outcome"].is_null());
    let rows = corrected(
        &[json!({"run_id":"tails","role":"review","attempts":[
            {"exit_code":1,"redacted_tail":"error: rate limit exceeded"},
            {"exit_code":0,"reason":"max_time"},
            {"exit_code":0,"status":"cancelled"},
            {"exit_code":0},
            {"exit_code":0,"content":false,"tool":true,"tree_changed":false,"verdict_usable":false,"tool_call_only":true}
        ]})],
        &[],
    );
    for (i, expected) in [
        "RATE_LIMIT",
        "MAX_TIME",
        pg_worker_core::outcome::Class::Neutral(pg_worker_core::outcome::Terminal::Cancelled)
            .as_str(),
    ]
    .iter()
    .enumerate()
    {
        assert_eq!(rows[i]["outcome"], *expected);
    }
    assert!(rows[3]["outcome"].is_null());
    assert_eq!(rows[4]["outcome"], "TOOL_CALL_ONLY");
}
#[test]
fn evidence_correction_beats_record() {
    let records = [json!({"run_id":"run-a","attempts":[{"attempt":0,"outcome":"CRASH","ts":10}]})];
    let rows = corrected(
        &records,
        &[json!({"run_id":"run-a","attempt":0,"outcome":"OK","ts":20})],
    );
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["outcome"], "OK");
}
#[test]
fn no_evidence_no_dependency() {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    assert!(read_rows(&dir).unwrap().is_empty());
}
#[test]
fn parked_prior_overlap_expired_two_until_outcome_leg() {
    let rows = [
        json!({"leg_key":"leg","kind":"omni_route_parked","ts":1,"until":20}),
        json!({"leg_key":"leg","kind":"omni_route_parked","ts":2,"until":30}),
        json!({"leg_key":"leg","kind":"omni_route_parked","ts":1,"until":5}),
    ];
    assert_eq!(observations(&rows, 10, 40).0.len(), 2);
}
#[test]
fn no_time_open_cleared_before_window_two_episodes() {
    let rows = [
        json!({"leg_key":"leg","kind":"omni_route_parked","ts":0,"first_seen":0,"state":"down"}),
        json!({"leg_key":"leg","kind":"omni_route_seen","ts":90000,"first_seen":0,"state":"down","phase":"cleared"}),
        json!({"leg_key":"leg","kind":"omni_route_parked","ts":100000,"first_seen":100000,"state":"down"}),
    ];
    assert_eq!(observations(&rows, 80000, 200000).1.len(), 2);
    assert_eq!(observations(&rows, 95000, 200000).1.len(), 1);
}
#[test]
fn stale_in_window() {
    assert_eq!(
        observations(
            &[json!({"kind":"omni_route_stale","leg_key":"leg","ts":10})],
            10,
            20
        )
        .2
        .len(),
        1
    );
}
#[test]
fn torn_tail_and_malformed_complete() {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir_all(dir.join("evidence")).unwrap();
    let path = dir.join("evidence/rows.jsonl");
    std::fs::write(&path, b"{}\n{broken").unwrap();
    assert_eq!(read_rows(&dir).unwrap().len(), 1);
    std::fs::write(&path, b"{}\n{broken\n").unwrap();
    assert_eq!(read_rows(&dir), Err(ReportError::InvalidRow));
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn legacy_classifier_unknown_not_guessed() {
    let rows = corrected(
        &[json!({"run_id":"run-a","status":"completed","attempts":[{}]})],
        &[],
    );
    assert!(rows[0]["outcome"].is_null());
}

#[test]
fn attempts_sort_numerically() {
    let evidence: Vec<_> = (0..13)
        .rev()
        .map(|n| json!({"run_id":"run", "attempt":n}))
        .collect();
    let rows = corrected(&[], &evidence);
    assert_eq!(
        rows.iter()
            .map(|r| r["attempt"].as_u64().unwrap())
            .collect::<Vec<_>>(),
        (0..13).collect::<Vec<_>>()
    );
    let records = [json!({"run_id":"run", "attempts":evidence})];
    assert_eq!(
        corrected(&records, &[])
            .iter()
            .map(|r| r["attempt"].as_u64().unwrap())
            .collect::<Vec<_>>(),
        (0..13).collect::<Vec<_>>()
    );
}
