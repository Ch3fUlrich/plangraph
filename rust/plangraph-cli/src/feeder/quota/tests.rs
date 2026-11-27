use super::*;
use serde_json::json;

fn snapshot(at: i64, rows: Value) -> String {
    json!({"observed_at":at,"rows":rows}).to_string()
}

fn codex(at: i64) -> Value {
    json!([{"provider":"codex:a","windowKey":"week","timestamp":at,"remainingPct":70}])
}

#[test]
fn center_snapshot_is_used_while_fresh_and_names_its_age_when_stale() {
    let now = 1_800_000_000;
    let rows = codex(now - 10);
    let fresh = snapshot(now - 900, rows.clone());
    assert_eq!(center_rows(Some(&fresh), now, 900, 10800).unwrap(), rows);
    for age in [901, -1] {
        let stale = snapshot(now - age, rows.clone());
        let error = center_rows(Some(&stale), now, 900, 10800).unwrap_err().0;
        assert!(error.starts_with("quota_stale:"), "{error}");
        assert!(
            error.contains(&format!("snapshot age {age}s (limit 900s)")),
            "{error}"
        );
        assert!(error.contains("codex observation age 10s"), "{error}");
    }
    for input in [None, Some("malformed"), Some("{\"rows\":[]}")] {
        let error = center_rows(input, now, 900, 10800).unwrap_err().0;
        assert!(error.contains("snapshot age unknown"), "{error}");
        assert!(error.contains("codex observation age unknown"), "{error}");
    }
}

#[test]
fn a_fresh_envelope_around_old_or_empty_observations_is_stale_quota_with_ages() {
    let now = 1_800_000_000;
    let old = snapshot(now - 30, codex(now - 10801));
    let empty = snapshot(now - 30, json!([]));
    for (doc, age) in [(old, "10801s"), (empty, "unknown")] {
        let error = center_rows(Some(&doc), now, 900, 10800).unwrap_err().0;
        assert!(
            error.starts_with("quota_stale:") && error.contains("snapshot age 30s"),
            "{error}"
        );
        assert!(
            error.contains(&format!("codex observation age {age} (limit 10800s)")),
            "{error}"
        );
    }
}

#[test]
fn a_host_without_codex_rows_reads_the_center_file_and_never_gets_null_quota() {
    let now = 1_800_000_000;
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("quota-facts.json");
    let rows = codex(now - 60);
    std::fs::write(&file, snapshot(now - 30, rows.clone())).unwrap();
    for local in [None, Some(json!([])), Some(json!([{"provider":"agy"}]))] {
        let selected = rows_for_host(local, None, &file, now, 900, 10800)
            .unwrap()
            .unwrap();
        assert_eq!(remaining(&selected, now, 10800), Some(70.0));
    }
    std::fs::remove_file(&file).unwrap();
    assert_eq!(
        rows_for_host(Some(rows.clone()), None, &file, now, 900, 10800).unwrap(),
        Some(rows)
    );
    assert_eq!(
        rows_for_host(None, Some(50.0), &file, now, 900, 10800).unwrap(),
        None
    );
    assert!(rows_for_host(None, None, &file, now, 900, 10800)
        .unwrap_err()
        .0
        .starts_with("quota_stale:"));
}
#[test]
fn aliases_exhaustion_freshness_and_account_isolation() {
    let now = 1_800_000_000;
    let row = |a: &str, w: &str, at: i64, q: Value| json!({"provider":format!("codex:{a}"),"windowKey":w,"timestamp":at,"remainingPct":q});
    let mut rows = vec![
        row("a", "day", now - 10, json!(90)),
        row("a", "week", now - 10, json!(30)),
        row("b", "day", now - 20, json!(99)),
        row("b", "day", now - 5, json!(2)),
        row("broken", "day", now - 5, json!("bad")),
    ];
    assert_eq!(remaining(&json!(rows), now, 10800), Some(30.0));
    rows[0]["isExhausted"] = json!("true");
    assert_eq!(remaining(&json!(rows), now, 10800), Some(2.0));
    rows[3]["isExhausted"] = json!("1");
    assert_eq!(remaining(&json!(rows), now, 10800), Some(0.0));
    assert_eq!(
        remaining(
            &json!([row("a", "day", now - 10801, json!(99))]),
            now,
            10800
        ),
        None
    );
    assert_eq!(
        remaining(&json!([row("a", "day", now + 1, json!(99))]), now, 10800),
        None
    );
    assert_eq!(remaining(&json!([]), now, 10800), None);
}
#[test]
fn newest_malformed_window_cannot_reuse_older_high_quota() {
    let rows = json!([
        {"provider":"codex","connection_id":"a","window_key":"day","created_at":"2026-01-01T00:00:00Z","remaining_percentage":99},
        {"provider":"codex","connection_id":"a","window_key":"day","created_at":"2026-01-01T00:00:01Z","remaining_percentage":null},
        {"provider":"codex","connection_id":"b","window_key":"day","created_at":"2026-01-01 00:00:01","remaining_percentage":40}
    ]);
    assert_eq!(remaining(&rows, 1767225602, 10800), Some(40.0));
}
#[test]
fn stamp_ns_reads_one_instant_alike_and_ranks_every_parseable_instant() {
    let iso = stamp_ns(&json!("2027-01-15T08:00:00.250000000")).unwrap();
    assert_eq!(stamp_ns(&json!(1_799_999_999.25 + 1.0)), Some(iso)); // seconds, a float fraction
    assert_eq!(stamp_ns(&json!(1_799_999_999_250.0 + 1_000.0)), Some(iso)); // milliseconds
    assert_eq!(stamp_ns(&json!("2027-01-15 08:00:00.25+00:00")), Some(iso));
    assert_eq!(stamp_ns(&json!("2027-01-15 09:00:00.25 +01:00")), Some(iso));
    // A fraction a float cannot hold keeps its decimal reading.
    assert_eq!(
        stamp_ns(&json!(1_800_000_000.1)),
        stamp_ns(&json!("2027-01-15T08:00:00.1"))
    );
    // Absurd instants rank at the ends, bounded, never lost.
    let (low, high) = (
        stamp_ns(&json!(-1e100)).unwrap(),
        stamp_ns(&json!(1e300)).unwrap(),
    );
    let future = stamp_ns(&json!("9999-12-31T00:00:00Z")).unwrap();
    assert!(low < stamp_ns(&json!(-1.0)).unwrap() && high > future && future > STAMP_MAX_NS);
    assert!(stamp_ns(&json!(-1.0)).unwrap() < 0 && iso < STAMP_MAX_NS);
    assert!(low > i128::MIN / 2 && high < i128::MAX / 2);
    assert_eq!(stamp_ns(&json!("not a time")), None);
    // The snapshot reader writes a numeric time as its decimal text; a JSON number could
    // not keep this fraction (default decoding moves it 200 ns).
    let ms = stamp_ns(&json!("1795581490000.0015")).unwrap();
    assert_eq!(ms, 1_795_581_490_000_001_500);
    assert_eq!(stamp_ns(&json!("1795581490.0000015")), Some(ms));
    assert_eq!(stamp_ns(&json!("1.5e-07")), Some(150));
    // Exponent text keeps every digit: a float would drop the last nanosecond here.
    assert_eq!(
        stamp_ns(&json!("1.795581490000001501e12")),
        Some(1_795_581_490_000_001_501)
    );
    assert_eq!(
        stamp_ns(&json!("17955814900000015.01e-4")),
        Some(1_795_581_490_000_001_501)
    );
    assert_eq!(stamp_ns(&json!("0.0000000000001e13")), Some(1_000_000_000));
    assert_eq!(stamp_ns(&json!("-1.5e0")), Some(-1_500_000_000));
    // An enormous exponent costs nothing: zero stays zero, a vanishing value is zero.
    for text in [
        "0e2147483647",
        "0e2147483630",
        "-0e2147483648",
        "0e99999999999999999999",
    ] {
        assert_eq!(stamp_ns(&json!(text)), Some(0), "{text}");
    }
    for text in ["1e-2147483648", "1e-99999999999", "1e-99999999999999999999"] {
        assert_eq!(stamp_ns(&json!(text)), Some(0), "{text}");
    }
    assert_eq!(
        stamp_ns(&json!("+1800000000.5")),
        stamp_ns(&json!(1_800_000_000.5))
    );
    for word in ["inf", "NaN", "e", "-", "."] {
        assert_eq!(stamp_ns(&json!(word)), None, "{word}");
    }
}
#[test]
fn the_legacy_readers_keep_an_empty_connection_id_apart_from_the_suffix_account() {
    let now = 1_800_000_000;
    let rows = json!([
        {"provider":"codex:a","connection_id":"","window_key":"day","created_at":now - 10,"remaining_percentage":80},
        {"provider":"codex","connection_id":"a","window_key":"week","created_at":now - 10,"remaining_percentage":10},
    ]);
    // Two accounts (the unnamed one and `a`): the pool takes the better, not the merged minimum.
    assert_eq!(remaining(&rows, now, 10_800), Some(80.0));
    assert_eq!(identity(&rows[0]), Some(("codex", "")));
    assert_eq!(pool_identity(&rows[0]), Some(("codex", "a")));
}
