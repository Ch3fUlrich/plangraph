use super::*;
#[test]
fn neutral_classes_reach_fold_and_unknowns_are_counted() {
    use pg_worker_core::outcome::Terminal;
    for terminal in [
        Terminal::CallerFault,
        Terminal::Cancelled,
        Terminal::Orphaned,
        Terminal::GitConfigMutated,
        Terminal::WriterNoChanges,
        Terminal::KillIncomplete,
        Terminal::ProofFailure,
    ] {
        let class = Class::Neutral(terminal);
        assert_eq!(parse_class(class.as_str()), Some(class));
    }
    let mut cancelled = row("cancelled", "cancelled", 30, 1);
    cancelled["reset_at"] = json!(900);
    let r = build_report(
        &[],
        &[
            row("crash", "CRASH", 10, 1),
            cancelled,
            row("unknown", "future_class", 40, 1),
        ],
        0,
        100,
    )
    .unwrap();
    assert_eq!(r["per_leg"]["leg"]["raw_reset_at"], json!([null, 900]));
    assert_eq!(r["per_leg"]["leg"]["strikes"], 1);
    assert_eq!(r["per_leg"]["leg"]["state"], "Closed");
    assert!(r["per_leg"]["leg"]["until"].is_null());
    assert_eq!(r["missing_fields"]["outcome"], 1);
}
#[test]
fn tool_open_without_s_eff_has_only_unknown_bounds() {
    let mut a = row("run-a", "HANG_SILENT", 1000, 1000);
    a["open_tools_at_kill"] = json!(1);
    let r = build_report(&[], &[a], 0, 2000).unwrap();
    let p = &r["silent_populations"];
    assert_eq!(p["tool_open"], 0);
    assert_eq!(p["k_count"], 0);
    assert!(p["k_fraction"].is_null());
    assert_eq!(p["tool_open_savings_hours"], 0.0);
    assert_eq!(p["unknown_tools"], 1);
    assert_eq!(p["unknown_lower_hours"], json!(615.01 / 3600.0));
    assert_eq!(p["unknown_upper_hours"], json!(1815.01 / 3600.0));
}
#[test]
fn classless_seats_false_kill_recovery() {
    let mut seat = row("seat", "", 20, 1);
    seat["outcome"] = Value::Null;
    seat["role"] = json!("review");
    let mut ok = row("kill", "OK", 40, 1);
    ok["attempt"] = json!(1);
    let r = build_report(
        &[],
        &[
            seat,
            row("kill", "HANG_SILENT", 30, 1),
            ok,
            row("sibling", "OK", 1830, 1),
            row("floor", "TOOL_CALL_ONLY", 50, 1),
        ],
        0,
        2000,
    )
    .unwrap();
    assert_eq!(r["totals"]["seat_deaths_without_class"], 1);
    for (key, denominator) in [
        ("tool_call_only_floor", 5),
        ("sibling_ok_within30min", 1),
        ("later_ok_fallthrough_recovery", 2),
    ] {
        assert_eq!(r[key]["count"], 1);
        assert_eq!(r[key]["denominator"], denominator);
        assert_eq!(r[key]["fraction"], json!(1.0 / denominator as f64));
    }
}
#[test]
fn chronological_leg_metadata() {
    let mut early = row("z", "CRASH", 10, 1);
    early["reset_at"] = json!(20);
    let r = build_report(&[], &[row("a", "CRASH", 30, 1), early], 0, 100).unwrap();
    assert_eq!(r["per_leg"]["leg"]["raw_reset_at"], json!([20, null]));
    let renamed = build_report(
        &[],
        &[row("z", "CRASH", 30, 1), row("a", "CRASH", 10, 1)],
        0,
        100,
    )
    .unwrap();
    assert_eq!(
        r["per_leg"]["leg"]["strikes"],
        renamed["per_leg"]["leg"]["strikes"]
    );
}
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
    a["gate"] = json!(false);
    let r = build_report(&[], &[a], 0, 20).unwrap();
    assert_eq!(r["totals"]["overrides"]["without_gate"], 1);
    assert!(r["baseline"].is_null());
}
#[test]
fn unknown_gate_not_a_skip() {
    let mut a = row("unknown", "OK", 10, 1);
    a["override"] = json!(true);
    let r = build_report(&[], &[a], 0, 20).unwrap();
    let o = &r["totals"]["overrides"];
    assert_eq!(o["unknown_gate"], 1);
    assert_eq!(o["without_gate"], 0);
    assert_eq!(o["gate_denominator"], 0);
    assert_eq!(o["skip_fraction"], 0.0);
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

#[test]
fn skip_fraction_over_all_launches() {
    let mut a = row("override", "OK", 10, 1);
    a["override"] = json!(true);
    a["gate"] = json!(false);
    let r = build_report(&[], &[a, row("ordinary", "OK", 11, 1)], 0, 20).unwrap();
    let o = &r["totals"]["overrides"];
    assert_eq!(o["without_gate"], 1);
    assert_eq!(o["gate_denominator"], 1);
    assert_eq!(o["skip_fraction"], 0.5);
    assert!(
        build_report(&[], &[], 0, 1).unwrap()["totals"]["overrides"]["skip_fraction"].is_null()
    );
}

#[test]
fn witnessed_share_collapse_is_reported_per_leg() {
    let until = 100_000;
    for (recent, total, expected) in [(0, 8, 1), (7, 8, 0), (0, 3, 0)] {
        let mut records = Vec::new();
        for (window, count, witnessed) in [(0, 10, 9), (1, total, recent)] {
            for index in 0..count {
                records.push(json!({"run_id":format!("run-{window}-{index}"),
                    "status":"completed", "ended":until - if window == 0 { 32400 } else { 10800 },
                    "chosen_leg":{"id":"mimo-fixture"},
                    "served_model":if index < witnessed { "mimo/mimo-v2.6-pro" } else { "unwitnessed" },
                    "witness_kind":if index < witnessed { "response-side-gateway-call-log" } else { "" }}));
            }
        }
        let report = build_report(&records, &[], 0, until).unwrap();
        let alerts = report["witness_alerts"]
            .as_array()
            .expect("witness alerts array");
        assert_eq!(alerts.len(), expected);
        if expected == 1 {
            assert!(alerts[0].as_str().unwrap().starts_with("ALERT "));
            assert!(alerts[0].as_str().unwrap().contains("mimo-fixture"));
        }
    }
}
