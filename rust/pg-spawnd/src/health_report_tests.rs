use super::*;
fn row(id: &str, class: &str, ts: i64, duration: i64) -> Value {
    json!({"run_id":id,"attempt":0,"outcome":class,"ts":ts,"duration_s":duration,"leg_key":"leg","route_key":"route","role":"research"})
}
#[test]
fn max_time_not_strike() {
    let r = build_report(&[], &[row("run-a", "MAX_TIME", 100, 20)], 0, 200).unwrap();
    assert_eq!(r["totals"]["max_time"], 1);
    assert_eq!(r["per_leg"]["leg"]["strikes"], 0);
}
#[test]
fn incident_not_row_count() {
    let r = build_report(
        &[],
        &[
            row("run-a", "CRASH", 100, 20),
            row("run-b", "CRASH", 110, 20),
        ],
        0,
        200,
    )
    .unwrap();
    assert_eq!(r["per_leg"]["leg"]["strikes"], 1);
}
#[test]
fn p50_p95_first_and_strong_missing() {
    let mut rows: Vec<_> = (1..=20)
        .map(|n| row(&format!("run-{n}"), "OK", n, 0))
        .collect();
    for (i, r) in rows.iter_mut().enumerate() {
        r["first_activity_s"] = json!(i + 1);
    }
    let r = build_report(&[], &rows, 0, 100).unwrap();
    assert_eq!(r["first_activity_s"]["p50"], 10.0);
    assert_eq!(r["first_activity_s"]["p95"], 19.0);
    assert!(r["time_to_first_strong_s"]["p50"].is_null());
}
#[test]
fn unusable_clipped_hours() {
    let r = build_report(&[], &[row("run-a", "CRASH", 7200, 7200)], 3600, 8000).unwrap();
    assert_eq!(r["totals"]["unusable_run_hours"], 1.0);
}
#[test]
fn tools1_1800_at1000s_still_tool_open() {
    let mut a = row("run-a", "HANG_SILENT", 1000, 1000);
    a["open_tools_at_kill"] = json!(1);
    a["s_eff_s"] = json!(1800);
    let r = build_report(&[], &[a], 0, 2000).unwrap();
    assert_eq!(r["silent_populations"]["tool_open"], 1);
}
#[test]
fn zero600_and_blind_null600_totals() {
    let mut a = row("run-a", "HANG_SILENT", 1000, 600);
    a["open_tools_at_kill"] = json!(0);
    let mut b = row("run-b", "HANG_SILENT", 1000, 600);
    b["s_eff_s"] = json!(600);
    let r = build_report(&[], &[a, b], 0, 2000).unwrap();
    assert_eq!(r["silent_populations"]["tools0"], 1);
    assert_eq!(r["silent_populations"]["blind"], 1);
}
#[test]
fn unknown_tool_bounds_and_k_count_fraction() {
    let r = build_report(&[], &[row("run-a", "HANG_SILENT", 100, 90)], 0, 200).unwrap();
    assert_eq!(r["silent_populations"]["unknown_tools"], 1);
    assert!(r["silent_populations"]["k_fraction"].is_null());
}
#[test]
fn baseline_target_override_gate_split() {
    let mut a = row("run-a", "OK", 10, 1);
    a["plan"] = json!({"p_source":"override"});
    let r = build_report(&[], &[a], 0, 20).unwrap();
    assert_eq!(r["totals"]["overrides"]["without_gate"], 1);
    assert!(r["baseline"].is_null());
}
#[test]
fn fixture_backfill_within_five_percent() {
    let records = vec![json!({"run_id":"run-a","attempts":[row("run-a","HANG_SILENT",3600,3600)]})];
    let r = build_report(&records, &[], 0, 7200).unwrap();
    for (field, expected) in [
        ("launches", 1.0),
        ("run_hours", 1.0),
        ("unusable_run_hours", 1.0),
        ("hang_hours", 1.0),
    ] {
        assert!((r["totals"][field].as_f64().unwrap() - expected).abs() / expected <= 0.05);
    }
}
