use super::tests::*;
use super::*;
use crate::events::{BlindReason, MAX_LINE};
use serde_json::{json, Value};

fn rate(info: Value, kind: Kind) {
    check(
        json!({"type":"rate_limit_event","rate_limit_info":info}),
        kind,
    );
}
#[test]
fn rate_limit_allowed_and_warning_are_request_not_error() {
    for status in ["allowed", "allowed_warning", "warning"] {
        rate(json!({"status":status}), Kind::Request);
    }
}
#[test]
fn rate_limit_rejected_status_is_error() {
    for status in ["rejected", "denied", "blocked", "exceeded", "limit_reached"] {
        for s in [status.to_owned(), status.to_uppercase()] {
            rate(json!({"status":s}), Kind::Error);
        }
    }
}
#[test]
fn rate_limit_usage_credit_or_daily_word_is_error() {
    for word in ["usage", "credit", "daily"] {
        for text in [word.to_owned(), word.to_uppercase()] {
            for key in [
                "message",
                "text",
                "error",
                "detail",
                "reason",
                "description",
            ] {
                rate(json!({"status":"allowed",key:text}), Kind::Error);
            }
        }
    }
}
#[test]
fn rate_limit_empty_missing_or_unknown_status_is_request() {
    for info in [json!({}), json!({"status":"new"}), json!({"status":null})] {
        rate(info, Kind::Request);
    }
    check(json!({"type":"rate_limit_event"}), Kind::Request);
}
#[test]
fn rate_limit_overage_fields_are_not_scanned() {
    rate(
        json!({"status":"allowed","overageStatus":"rejected",
        "overageDisabledReason":"out_of_credits","rateLimitType":"five_hour"}),
        Kind::Request,
    );
}
#[test]
fn rate_limit_event_after_result_is_still_parsed() {
    let mut p = parser(Flavor::Claude);
    for (event, kind) in [
        (json!({"type":"result"}), Kind::Request),
        (
            json!({"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}),
            Kind::Request,
        ),
        (
            json!({"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}),
            Kind::Error,
        ),
    ] {
        assert_eq!(kinds(&send(&mut p, event)), vec![(kind, None)]);
    }
    assert_eq!(p.health.unknown(), 0);
}
#[test]
fn rate_limit_flat_event_without_info_object_uses_top_level_status() {
    for info in [Value::Null, json!(3), json!("ignored")] {
        check(
            json!({"type":"rate_limit_event","status":"denied","rate_limit_info":info}),
            Kind::Error,
        );
    }
    check(
        json!({"type":"rate_limit_event","status":"blocked"}),
        Kind::Error,
    );
}
#[test]
fn version_drift_is_blind_and_recorded_versions_are_not() {
    for (flavor, versions) in [
        (Flavor::Claude, RECORDED_CLAUDE),
        (Flavor::Qodercli, RECORDED_QODERCLI),
    ] {
        for version in versions {
            assert_eq!(
                ClaudeParser::new(flavor, 0, Some(180), version)
                    .health
                    .blind(0),
                None
            );
        }
        for version in ["0.0.0", ""] {
            assert_eq!(
                ClaudeParser::new(flavor, 0, Some(180), version)
                    .health
                    .blind(0),
                Some(BlindReason::VersionDrift)
            );
        }
    }
}
#[test]
fn chunk_split_anywhere_equals_one_chunk() {
    let mut whole = parser(Flavor::Claude);
    let expected = whole.feed(TOOL.as_bytes(), 10);
    let mut split = parser(Flavor::Claude);
    let actual: Vec<_> = TOOL
        .as_bytes()
        .iter()
        .flat_map(|b| split.feed(&[*b], 10))
        .collect();
    assert_eq!(kinds(&actual), kinds(&expected));
    assert_eq!(
        actual.iter().map(|s| s.ts_ms).collect::<Vec<_>>(),
        expected.iter().map(|s| s.ts_ms).collect::<Vec<_>>()
    );
    assert_eq!(split.health.lines(), whole.health.lines());
    assert_eq!(split.health.bad(), whole.health.bad());
    assert_eq!(split.health.unknown(), whole.health.unknown());
}
#[test]
fn finish_input_parses_unterminated_tail() {
    let mut p = parser(Flavor::Claude);
    assert!(p
        .feed(json!({"type":"result"}).to_string().as_bytes(), 1)
        .is_empty());
    let signals = p.finish_input(50);
    assert_eq!(kinds(&signals), vec![(Kind::Request, None)]);
    assert_eq!(signals[0].ts_ms, 50);
    assert!(p.finish_input(51).is_empty());
}
#[test]
fn non_json_and_overlong_lines_are_bad_without_signals() {
    let mut p = parser(Flavor::Claude);
    for bytes in [b"broken\n".to_vec(), b"[]\n".to_vec(), vec![255, 10]] {
        assert!(p.feed(&bytes, 10).is_empty());
    }
    let mut overlong = vec![b'x'; MAX_LINE + 1];
    overlong.push(b'\n');
    assert!(p.feed(&overlong, 10).is_empty());
    for _ in 0..7 {
        assert!(p.feed(b"bad\n", 10).is_empty());
    }
    for _ in 0..9 {
        send(&mut p, json!({"type":"result"}));
    }
    assert_eq!((p.health.lines(), p.health.bad()), (20, 11));
    assert_eq!(p.health.blind(10), Some(BlindReason::BadRatio));
}
#[test]
fn signal_times_are_arrival_not_event_fields() {
    let mut p = parser(Flavor::Claude);
    let line = json!({"type":"result","ts_ms":999,"timestamp":888}).to_string();
    let signals = p.feed(format!("{line}\n").as_bytes(), 42);
    assert_eq!(signals.len(), 1);
    assert_eq!(signals[0].ts_ms, 42);
}
#[test]
fn parser_keeps_no_payload_text() {
    let mut p = parser(Flavor::Claude);
    let debug = format!("{:?}", p.feed(TOOL.as_bytes(), 10));
    let mut values = Vec::new();
    for line in TOOL.lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        if let Some(blocks) = v["message"]["content"].as_array() {
            for block in blocks {
                for key in ["text", "thinking", "content"] {
                    if let Some(text) = block[key].as_str() {
                        values.push(text.to_owned());
                    }
                }
                if let Some(input) = block["input"].as_object() {
                    values.extend(input.values().filter_map(Value::as_str).map(str::to_owned));
                }
            }
        }
    }
    values.retain(|text| !text.is_empty());
    assert!(!values.is_empty());
    for text in values {
        assert!(!debug.contains(&text));
    }
}
