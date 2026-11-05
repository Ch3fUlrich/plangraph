use super::{tests::*, *};
use crate::{
    hosts::Role,
    outcome::{classify, Class, Outcome, Scope},
};

fn outcome(i: &Input<'_>) -> Outcome {
    let mut f = Facts::new(Role::Orchestrate, i.exit);
    f.content = true;
    f.tool = true;
    if let Some(hit) = scan(i) {
        f = f.with_hit(hit);
    }
    classify(&f)
}

#[test]
fn omniroute_503_after_content_is_provider_unavailable_route_scope() {
    let mut i = input("", "HTTP 503: service temporarily unavailable");
    i.headers = &[("Retry-After", "30")];
    let o = outcome(&i);
    assert_eq!(o.class, Class::ProviderUnavailable);
    assert_eq!(o.class.scope(), Scope::Route);
    assert_eq!(o.reset_at, Some(i.ts + 30.0));
}

#[test]
fn all_targets_skipped_and_circuit_open_lines() {
    for line in [
        "Error: all targets were skipped",
        "Error: circuit breaker is open",
        "HTTP 502: bad gateway",
        "HTTP 504: overloaded",
    ] {
        assert_eq!(outcome(&input("", line)).class, Class::ProviderUnavailable);
    }
}

#[test]
fn retry_after_captured_for_auth_and_model_unavailable_too() {
    for (line, class) in [
        ("Error: authentication failed", Class::Auth),
        ("Model unavailable", Class::ModelUnavailable),
    ] {
        let mut i = input("", line);
        i.headers = &[("Retry-After", "30")];
        let o = outcome(&i);
        assert_eq!(o.class, class);
        assert_eq!(o.reset_at, Some(i.ts + 30.0));
    }
}

#[test]
fn three_day_retry_after_keeps_raw_reset_with_clamped() {
    let mut i = input("", "HTTP 503: service temporarily unavailable");
    i.headers = &[("Retry-After", "259200")];
    let o = outcome(&i);
    assert_eq!(o.class, Class::ProviderUnavailable);
    assert_eq!(o.reset_at, Some(i.ts + 259200.0));
    assert!(o.reset_clamped);
}

#[test]
fn model_unavailable_stderr_and_error_json_line() {
    for (stdout, stderr) in [
        ("", "diagnostic: Model unavailable"),
        (r#"{"error":"not_served_on_host"}"#, ""),
        (r#"  "error": "unknown model""#, ""),
        ("Error: not served", ""),
    ] {
        assert_eq!(
            outcome(&input(stdout, stderr)).class,
            Class::ModelUnavailable
        );
    }
}

#[test]
fn auth_credit_marker_wins_over_quota_words() {
    assert_eq!(
        outcome(&input("", "Error: insufficient credits, quota exceeded")).class,
        Class::Auth
    );
}

#[test]
fn opencode_429_free_tier_fixture_is_quota() {
    assert_eq!(outcome(&input(FREE, "")).class, Class::Quota);
}

#[test]
fn opencode_429_without_quota_word_is_rate_limit() {
    assert_eq!(outcome(&input(TRANSIENT, "")).class, Class::RateLimit);
}

#[test]
fn deepseek_recording_is_quota_not_transient() {
    // Both recorded 429 errors have this same type and message.
    assert_eq!(outcome(&input(FREE, "")).class, Class::Quota);
}

#[test]
fn bare_http_429_is_rate_limit() {
    assert_eq!(outcome(&input("", "HTTP 429")).class, Class::RateLimit);
}

#[test]
fn agy_quota_line_with_resets_in() {
    for stderr in [false, true] {
        let line = "AGY_ERROR 429: Resets in 2h 5m";
        let i = if stderr {
            input("", line)
        } else {
            input(line, "")
        };
        let o = outcome(&i);
        assert_eq!(o.class, Class::Quota);
        assert_eq!(o.reset_at, Some(i.ts + 7500.0));
    }
}

#[test]
fn claude_rate_limit_event_allowed_warning_on_exit_zero_is_ok() {
    let mut i = input(
        r#"{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning"},"message":"credit usage limit"}"#,
        "",
    );
    i.exit = 0;
    assert!(scan(&i).is_none());
    assert_eq!(outcome(&i).class, Class::Ok);
}

#[test]
fn claude_rate_limit_event_denying_is_quota_with_resets_at() {
    let mut i = input(
        r#"{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":2000000030}}"#,
        "",
    );
    i.exit = 0;
    i.response_date = Some(2000000000.0);
    let o = outcome(&i);
    assert_eq!(o.class, Class::Quota);
    assert_eq!(o.reset_at, Some(i.ts + 30.0));
    i.response_date = None;
    assert_eq!(outcome(&i).reset_at, None);
}

#[test]
fn healthy_exit_zero_prose_about_rate_limit_is_unmarked() {
    let mut i = input("We discuss rate limit and quota", "Error: rate limit");
    i.exit = 0;
    assert!(scan(&i).is_none());
    assert_eq!(outcome(&i).class, Class::Ok);
}

#[test]
fn lone_step_start_exit_zero_is_empty() {
    let mut i = input(r#"{"type":"step_start"}"#, "");
    i.exit = 0;
    assert!(scan(&i).is_none());
    assert_eq!(
        classify(&Facts::new(Role::Orchestrate, i.exit)).class,
        Class::Empty
    );
}

#[test]
fn nonzero_exit_without_marker_is_crash() {
    let line = r#"{"type":"error","error":{"type":"unknown","message":"Transport: The socket connection was closed unexpectedly. For more information, pass `verbose: true` in the second argument to fetch()"}}"#;
    let i = input(line, "");
    assert!(scan(&i).is_none());
    assert_eq!(
        classify(&Facts::new(Role::Orchestrate, i.exit)).class,
        Class::Crash
    );
}

#[test]
fn with_hit_fills_outcome_reset_raw_and_evidence() {
    let mut i = input("Error: rate limit (reset after 259200s)\nResets in 1s", "");
    let hit = scan(&i).unwrap();
    let f = Facts::new(Role::Orchestrate, i.exit).with_hit(hit);
    assert_eq!(f.marker, Some(Marker::RateLimit));
    let o = classify(&f);
    assert_eq!(o.class, Class::RateLimit);
    assert_eq!(o.reset_at, Some(i.ts + 259200.0));
    assert!(o.reset_clamped);
    assert_eq!(
        o.evidence.as_deref(),
        Some("Error: rate limit (reset after 259200s)")
    );
    i.stdout = "Error: rate limit\nResets in 1s";
    assert_eq!(outcome(&i).reset_at, None);
}

#[test]
fn structured_503_exit_zero_does_not_open_route() {
    let mut i = input(r#"{"type":"error","error":{"status":503}}"#, "");
    i.exit = 0;
    assert!(scan(&i).is_some());
    assert_eq!(outcome(&i).class, Class::Ok);
}
