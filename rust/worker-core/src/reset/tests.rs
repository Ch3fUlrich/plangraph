use super::*;

const TS: f64 = 1_800_000_000.0;

fn check(headers: &[(&str, &str)], body: &str, date: Option<f64>, delta: f64) {
    assert_eq!(
        capture(headers, body, date, TS),
        Some(Reset {
            reset_at: TS + delta,
            clamped: delta > CLAMP_S,
        })
    );
}

#[test]
fn retry_after_seconds_and_skewed_http_date() {
    check(&[("Retry-After", "12")], "", None, 12.0);
    let date = http_date("Sun, 06 Nov 1994 08:49:37 GMT").unwrap();
    let ts = date + 3600.0;
    assert_eq!(
        capture(
            &[("retry-after", "Sun, 06 Nov 1994 10:49:37 GMT")],
            "",
            Some(date),
            ts
        )
        .unwrap()
        .reset_at,
        ts + 7200.0
    );
}

#[test]
fn reset_headers_absolute_relative_and_compound_units() {
    check(&[("x-ratelimit-reset", "1800000060")], "", Some(TS), 60.0);
    check(&[("x-ratelimit-reset", "42")], "", None, 42.0);
    check(&[("x-ratelimit-reset-requests", "6m0s")], "", None, 360.0);
    check(&[("x-ratelimit-reset-tokens", "20ms")], "", None, 0.02);
    check(&[("x-ratelimit-reset", "1h2m3.5s")], "", None, 3723.5);
    check(
        &[("x-ratelimit-reset", "2026-11-03T04:00:00Z")],
        "",
        Some(http_date("2026-11-03T03:00:00Z").unwrap()),
        3600.0,
    );
}

#[test]
fn prose_units_and_agy() {
    for unit in ["ms", "msec", "msecs", "millisecond", "milliseconds"] {
        check(&[], &format!("resets in 500{unit}"), None, 0.5);
    }
    check(&[], "resets in 1h2m", None, 3720.0);
    for (body, delta) in [
        ("resets in 2h", 7200.0),
        ("reset after 3h 12m 5s", 11525.0),
        ("resets in 500ms", 0.5),
        ("Resets in 1h 30m", 5400.0),
    ] {
        check(&[], body, None, delta);
    }
    assert_eq!(capture(&[], "resets in 2h 3frogs", None, TS), None);
}

#[test]
fn body_fields_claude_and_omniroute() {
    check(&[], r#"{"resetsAt":1800000060}"#, Some(TS), 60.0);
    check(&[], r#""resetsAt":1800000060000"#, Some(TS), 60.0);
    assert_eq!(capture(&[], r#"{"resetsAt":1800000060}"#, None, TS), None);
    check(&[], r#""retryAfterMs":2500"#, None, 2.5);
}

#[test]
fn precedence_first_header_and_fallthrough() {
    check(
        &[("retry-after", "7"), ("Retry-After", "9")],
        "resets in 2h",
        None,
        7.0,
    );
    check(
        &[("retry-after", "bad"), ("x-ratelimit-reset", "8")],
        "",
        None,
        8.0,
    );
    check(
        &[("x-ratelimit-reset", "1800000060")],
        "resets in 1s",
        None,
        1.0,
    );
    check(
        &[("retry-after", "bad"), ("Retry-After", "9")],
        "resets in 1s",
        None,
        1.0,
    );
}

#[test]
fn dateless_past_zero_and_invalid_values() {
    assert_eq!(
        capture(&[("x-ratelimit-reset", "1800000060")], "", None, TS),
        None
    );
    check(
        &[("x-ratelimit-reset", "1800000000")],
        "",
        Some(TS + 10.0),
        0.0,
    );
    check(&[("retry-after", "0")], "", None, 0.0);
    for value in ["-1", "NaN", "inf", "1e999"] {
        assert_eq!(capture(&[("retry-after", value)], "", None, TS), None);
    }
}

#[test]
fn raw_three_days_and_strict_clamp_boundary() {
    check(&[("retry-after", "259200")], "", None, 259200.0);
    assert_eq!(clamp_until(TS + 259200.0, TS), (TS + CLAMP_S, true));
    check(&[("retry-after", "86400")], "", None, CLAMP_S);
    assert_eq!(clamp_until(TS + CLAMP_S, TS), (TS + CLAMP_S, false));
}

#[test]
fn http_dates_valid_offsets_fractions_and_invalid_fields() {
    assert_eq!(
        http_date("Sun, 06 Nov 1994 08:49:37 GMT"),
        Some(784111777.0)
    );
    assert_eq!(
        http_date("2026-11-03T04:00:00.25+02:30"),
        http_date("2026-11-03T01:30:00.25Z")
    );
    assert_eq!(
        http_date("2026-11-03T04:00:00-02:30"),
        http_date("2026-11-03T06:30:00Z")
    );
    assert!(http_date("2024-02-29T00:00:00Z").is_some());
    for value in [
        "2026-13-01T00:00:00Z",
        "2026-04-31T00:00:00Z",
        "2026-02-29T00:00:00Z",
        "2026-11-03T24:00:00Z",
        "Sun, 31 Apr 1994 08:49:37 GMT",
    ] {
        assert_eq!(http_date(value), None);
    }
}
