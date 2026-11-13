use super::*;
use serde_json::json;

fn input() -> Value {
    json!({"now":10000,"sessions":[{"session_id":"session-1","role":"build",
        "busy":true,"transcript_mtime":8740}],
        "runs":[{"run_id":"run-last","parent_session":"session-1","ended":9000}]})
}

fn alerts(input: &Value) -> Vec<Alert> {
    let found = Reader {
        session_activity: Block::Present(input.clone()),
        ..Default::default()
    }
    .alerts();
    for alert in &found {
        assert!(alert.message.starts_with(PREFIX), "{}", alert.message);
        assert!(alert.active.starts_with(PREFIX), "{}", alert.active);
    }
    found
}

#[test]
fn alert_messages_and_identities_share_the_prefix() {
    let quiet = alerts(&input());
    assert_eq!(quiet.len(), 1);
    assert_eq!(quiet[0].message, format!("{PREFIX}session=\"session-1\" role=\"build\" quiet_age_seconds=1260 last_ended_run=\"run-last\""));
    assert_eq!(
        quiet[0].active,
        format!("{PREFIX}session=\"session-1\" transcript_mtime=8740")
    );
    for (field, value, detail) in [
        ("now", Value::Null, "unknown=clock"),
        ("sessions", Value::Null, "unknown=sessions"),
        (
            "runs",
            Value::Null,
            "session=\"session-1\" role=\"build\" unknown=runs",
        ),
    ] {
        let mut s = input();
        s[field] = value;
        let found = alerts(&s);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].message, format!("{PREFIX}{detail}"));
    }
    let reader = alerts(&Value::Null);
    assert_eq!(reader.len(), 1);
    assert_eq!(reader[0].message, format!("{PREFIX}unknown=reader"));
    assert_eq!(reader[0].active, reader[0].message);
}

#[test]
fn collectors_heading_is_separate_from_delivery_paragraph() {
    let doc = include_str!("../../../../../docs/flow-watchdog.md").replace("\r\n", "\n");
    assert!(doc.contains(concat!(
        "overflow on stderr.\n\n## Collectors, freshness and host integration\n\n",
        "Collectors, freshness and host integration are deployment prerequisites, not supplied by this checker."
    )));
}

#[test]
fn quiet_threshold_has_negative_and_positive_controls() {
    let mut s = input();
    s["runs"] = json!([]); // Empty, readable run evidence still qualifies.
    for seconds in [1140.0, 1200.0, 1201.0, 1200.5, 1200.001] {
        s["sessions"][0]["transcript_mtime"] = (10000.0 - seconds).into();
        let found = alerts(&s);
        if seconds <= 1200.0 {
            assert!(found.is_empty());
        } else {
            assert_eq!(found.len(), 1);
            assert!(found[0].message.contains("quiet_age_seconds=1201 "));
            assert!(found[0].message.ends_with("last_ended_run=none"));
        }
    }
}

#[test]
fn known_idle_rows_skip_missing_empty_or_malformed_session_details() {
    // docs/flow-watchdog.md and the round-1 changelog: skip known idle rows
    // before evidence validation; unreadable transcript is unknown for busy rows.
    for row in json!([
        {"busy":false,"session_id":"session-1","role":"build","transcript_mtime":8740},
        {"busy":false}, {"busy":false,"session_id":"","role":""},
        {"busy":false,"session_id":null,"role":17,"transcript_mtime":"bad"},
        {"busy":false,"session_id":[],"role":{}}
    ])
    .as_array()
    .unwrap()
    {
        let mut s = input();
        s["sessions"] = json!([row]);
        assert!(alerts(&s).is_empty(), "{s}");
    }
}

#[test]
fn indeterminate_idleness_or_invalid_busy_session_details_are_unknown() {
    let rows = json!([null, {}, {"busy":"false"}, {"busy":null}, {"busy":true},
        {"busy":true,"session_id":"","role":"build"},
        {"busy":true,"session_id":"session-1","role":false},
        {"session_id":"session-1","role":"build"},
        {"busy":null,"session_id":"session-1","role":"build"},
        {"busy":"","session_id":"session-1","role":"build"},
        {"busy":17,"session_id":"session-1","role":"build"},
        {"busy":[],"session_id":"session-1","role":"build"},
        {"busy":true,"session_id":"session-1"},
        {"busy":true,"session_id":"session-1","role":null},
        {"busy":true,"session_id":"session-1","role":""},
        {"busy":true,"session_id":"session-1","role":17},
        {"busy":true,"session_id":"session-1","role":[]}]);
    for row in rows.as_array().unwrap() {
        let context = row
            .get("session_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(|id| format!("session={id:?} "))
            .unwrap_or_default();
        let mut s = input();
        s["sessions"] = json!([row]);
        let found = alerts(&s);
        assert_eq!(found.len(), 1);
        assert_eq!(
            found[0].message,
            format!("{PREFIX}{context}unknown=sessions")
        );
        assert_eq!(found[0].active, found[0].message);
    }
}

#[test]
fn conflicting_session_rows_report_one_stable_unknown_in_either_order() {
    for (field, conflict) in [
        ("transcript_mtime", json!(8700)),
        ("role", json!("review")),
        ("busy", json!(false)),
        ("transcript_mtime", Value::Null),
    ] {
        let mut s = input();
        let mut other = s["sessions"][0].clone();
        other[field] = conflict;
        s["sessions"].as_array_mut().unwrap().push(other);
        for _ in 0..2 {
            let found = alerts(&s);
            assert_eq!(found.len(), 1);
            assert_eq!(
                found[0].message,
                format!("{PREFIX}session=\"session-1\" unknown=sessions")
            );
            assert_eq!(found[0].active, found[0].message);
            s["sessions"].as_array_mut().unwrap().reverse();
        }
    }
}

#[test]
fn condition_local_unknowns_outrank_live_own_run_suppression() {
    for (transcript, runs, reason) in [
        (true, false, "transcript"),
        (false, true, "runs"),
        (true, true, "transcript,runs"),
    ] {
        let mut s = input();
        s["runs"][0]["ended"] = Value::Null;
        if transcript {
            s["sessions"][0]["transcript_mtime"] = Value::Null;
        }
        if runs {
            s["runs"]
                .as_array_mut()
                .unwrap()
                .push(json!({"parent_session":"session-1"}));
        }
        let found = alerts(&s);
        assert_eq!(found.len(), 1);
        assert_eq!(
            found[0].active,
            format!("{PREFIX}session=\"session-1\" unknown={reason}")
        );
        s["now"] = json!(10060);
        assert_eq!(alerts(&s)[0].active, found[0].active);
        s["sessions"][0]["transcript_mtime"] = json!(8740);
        if runs {
            s["runs"].as_array_mut().unwrap().pop();
        }
        assert!(alerts(&s).is_empty());
    }
}

#[test]
fn changing_bad_run_record_index_does_not_refire_unknown_evidence() {
    for record in [
        json!({"run_id":"broken"}),
        json!({"parent_session":"session-1"}),
    ] {
        let mut s = input();
        s["runs"].as_array_mut().unwrap().push(record);
        let first = alerts(&s);
        assert_eq!(first.len(), 1);
        assert!(first[0]
            .message
            .ends_with("unknown=runs run_record_index=1"));
        s["runs"].as_array_mut().unwrap().reverse();
        let next = alerts(&s);
        assert!(next[0].message.ends_with("unknown=runs run_record_index=0"));
        assert_eq!(next[0].active, first[0].active);
    }
}
