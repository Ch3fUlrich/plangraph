use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};

const NOW: u64 = 10_000;

fn snapshot() -> Value {
    json!({"pace":1,"budget":2,"ci_failed":0,"ci_total":0,
        "infra_failed":0,"review_age_hours":0,"review_rounds":0,
        "critical_idle_hours":0,"coding_vm_runs":0,"worker_idle":false,
        "host_drift":false,"closes_recent":0,"closes_previous":0,
        "session_activity":{"now":NOW,"sessions":[{"session_id":"session-1",
            "role":"build","busy":true,"transcript_mtime":NOW - 21 * 60}],
            "runs":[{"run_id":"run-first","parent_session":"session-1","ended":8800},
                {"run_id":"run-last","parent_session":"session-1","ended":9000},
                {"run_id":"run-other","parent_session":"session-2","ended":null}]}})
}

fn pass(dir: &Path, snapshot: &Value) -> Vec<Value> {
    let input = dir.join("input.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .arg(input)
        .arg(dir)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", dir)
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    fs::read(dir.join("queue.json"))
        .map(|bytes| serde_json::from_slice(&bytes).unwrap())
        .unwrap_or_default()
}

fn events(snapshot: &Value) -> Vec<Value> {
    pass(tempfile::tempdir().unwrap().path(), snapshot)
}

fn line(events: &[Value]) -> &str {
    assert_eq!(events.len(), 1, "{events:?}");
    assert_eq!(events[0]["kind"], "alert");
    assert_eq!(events[0]["recipient"], "routing");
    let line = events[0]["message"].as_str().unwrap();
    assert_eq!(line.lines().count(), 1);
    assert!(line.starts_with("FLOW-WATCHDOG: busy-quiet "), "{line}");
    line
}

#[test]
fn quiet_21_minutes_and_all_own_runs_ended_alerts_once_with_latest_end() {
    let mut s = snapshot();
    for _ in 0..2 {
        let found = events(&s);
        assert_eq!(line(&found), "FLOW-WATCHDOG: busy-quiet session=\"session-1\" role=\"build\" quiet_age_seconds=1260 last_ended_run=\"run-last\"");
        s["session_activity"]["runs"]
            .as_array_mut()
            .unwrap()
            .reverse();
    }
}

#[test]
fn equal_end_times_choose_the_greatest_run_id_in_either_order() {
    let mut s = snapshot();
    s["session_activity"]["runs"][0]["ended"] = 9000.into();
    for _ in 0..2 {
        assert!(line(&events(&s)).ends_with("last_ended_run=\"run-last\""));
        s["session_activity"]["runs"]
            .as_array_mut()
            .unwrap()
            .reverse();
    }
}

#[test]
fn one_own_live_run_prevents_alert_until_it_ends_without_swallowing_first_alert() {
    let dir = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    s["session_activity"]["runs"][1]["ended"] = Value::Null;
    assert!(pass(dir.path(), &s).is_empty());
    assert_eq!(
        fs::read_to_string(dir.path().join("active")).unwrap_or_default(),
        ""
    );
    s["session_activity"]["runs"][1]["ended"] = NOW.into();
    let found = pass(dir.path(), &s);
    assert!(line(&found).ends_with("last_ended_run=\"run-last\""));
    assert_eq!(pass(dir.path(), &s), found);
}

#[test]
fn live_own_run_clears_a_standing_identity_and_ending_it_can_alert_again() {
    let dir = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    let first = pass(dir.path(), &s);
    line(&first);
    s["session_activity"]["runs"][1]["ended"] = Value::Null;
    assert_eq!(pass(dir.path(), &s), first);
    assert_eq!(fs::read_to_string(dir.path().join("active")).unwrap(), "");
    s["session_activity"]["runs"][1]["ended"] = 9000.into();
    let found = pass(dir.path(), &s);
    assert_eq!(found.len(), 2);
    assert_eq!(found[0], found[1]);
    assert_eq!(pass(dir.path(), &s), found);
}

#[test]
fn quiet_19_minutes_and_exactly_20_minutes_do_not_alert() {
    for seconds in [19 * 60, 20 * 60] {
        let mut s = snapshot();
        s["session_activity"]["sessions"][0]["transcript_mtime"] = (NOW - seconds).into();
        let found = events(&s);
        assert!(
            found.iter().all(|event| {
                let message = event["message"].as_str().unwrap();
                !message.contains("busy-quiet") && !message.contains("unknown=")
            }),
            "{found:?}"
        );
    }
}

#[test]
fn just_over_20_minutes_alerts_with_displayed_age_above_the_strict_threshold() {
    for seconds in [1201.0, 1200.5, 1200.001] {
        let mut s = snapshot();
        s["session_activity"]["sessions"][0]["transcript_mtime"] = (NOW as f64 - seconds).into();
        assert!(line(&events(&s)).contains("quiet_age_seconds=1201 "));
    }
}

#[test]
fn idle_session_does_not_alert() {
    let mut s = snapshot();
    s["session_activity"]["sessions"][0]["busy"] = false.into();
    assert!(events(&s).is_empty());
}

#[test]
fn known_idle_rows_skip_missing_empty_or_malformed_session_details() {
    for row in [
        json!({"busy":false}),
        json!({"busy":false,"session_id":"","role":""}),
        json!({"busy":false,"session_id":null,"role":17,"transcript_mtime":"bad"}),
        json!({"busy":false,"session_id":[],"role":{}}),
    ] {
        let mut s = snapshot();
        s["session_activity"]["sessions"] = json!([row]);
        assert!(events(&s).is_empty(), "{s}");
    }
}

#[test]
fn indeterminate_idleness_or_invalid_busy_session_details_are_unknown() {
    for row in [
        Value::Null,
        json!({}),
        json!({"busy":"false"}),
        json!({"busy":null}),
        json!({"busy":true}),
        json!({"busy":true,"session_id":"","role":"build"}),
        json!({"busy":true,"session_id":"session-1","role":false}),
    ] {
        let mut s = snapshot();
        s["session_activity"]["sessions"] = json!([row]);
        assert_eq!(
            line(&events(&s)),
            "FLOW-WATCHDOG: busy-quiet unknown=sessions"
        );
    }
}

#[test]
fn busy_quiet_session_with_no_runs_alerts() {
    let mut s = snapshot();
    s["session_activity"]["runs"] = json!([]);
    let found = events(&s);
    assert!(line(&found).ends_with("quiet_age_seconds=1260 last_ended_run=none"));
}

#[test]
fn repeated_pass_with_increasing_age_keeps_the_same_quiet_period_deduplicated() {
    let dir = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    let first = pass(dir.path(), &s);
    assert_eq!(first.len(), 1);
    s["session_activity"]["now"] = (NOW + 14 * 60).into();
    s["session_activity"]["sessions"][0]["role"] = "review".into();
    s["session_activity"]["runs"][1]["run_id"] = "run-renamed".into();
    assert_eq!(pass(dir.path(), &s), first);
    // Transcript progress starts a new quiet period, even if no tick saw recovery.
    s["session_activity"]["sessions"][0]["transcript_mtime"] = (NOW - 1).into();
    s["session_activity"]["now"] = (NOW + 21 * 60).into();
    let next = pass(dir.path(), &s);
    assert_eq!(next.len(), 2);
    assert!(next[1]["message"]
        .as_str()
        .unwrap()
        .contains("quiet_age_seconds=1261"));
}

#[test]
fn recovery_clears_existing_dedupe_before_refiring() {
    let dir = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    assert_eq!(pass(dir.path(), &s).len(), 1);
    s["session_activity"]["sessions"][0]["busy"] = false.into();
    pass(dir.path(), &s);
    assert_eq!(fs::read_to_string(dir.path().join("active")).unwrap(), "");
    s["session_activity"]["sessions"][0]["busy"] = true.into();
    assert_eq!(pass(dir.path(), &s).len(), 2);
}

#[test]
fn unreadable_transcript_is_unknown_in_its_own_deduplicated_line() {
    for evidence in [Value::Null, json!({"error":"unreadable"}), json!("bad")] {
        let dir = tempfile::tempdir().unwrap();
        let mut s = snapshot();
        s["session_activity"]["sessions"][0]["transcript_mtime"] = evidence;
        s["host_drift"] = true.into();
        let found = pass(dir.path(), &s);
        assert!(found[0]["message"]
            .as_str()
            .unwrap()
            .starts_with("FLOW-WATCHDOG: host-drift"));
        let quiet: Vec<_> = found
            .iter()
            .filter(|event| {
                event["message"]
                    .as_str()
                    .unwrap()
                    .starts_with("FLOW-WATCHDOG: busy-quiet ")
            })
            .cloned()
            .collect();
        assert!(line(&quiet).contains("session=\"session-1\" role=\"build\" unknown=transcript"));
        s["session_activity"]["now"] = (NOW + 60).into();
        assert_eq!(pass(dir.path(), &s), found);
    }
}

#[test]
fn unreadable_run_records_never_count_as_ended_or_an_empty_list() {
    for runs in [
        Value::Null,
        json!({"error":"unreadable"}),
        json!([null]),
        json!([{"run_id":"broken","parent_session":"session-1","ended":"bad"}]),
        json!([{"run_id":"broken","parent_session":"session-1"}]),
        json!([{"run_id":"broken","ended":9000}]),
    ] {
        let mut s = snapshot();
        s["session_activity"]["runs"] = runs;
        let found = events(&s);
        assert_eq!(found.len(), 2, "{found:?}");
        assert!(line(&found[..1]).contains("session=\"session-1\" role=\"build\" unknown=runs"));
        assert_eq!(line(&found[1..]), "FLOW-WATCHDOG: busy-quiet unknown=runs");
    }
}

#[test]
fn missing_or_unreadable_run_block_is_unknown_even_without_busy_sessions() {
    for runs in [
        None,
        Some(Value::Null),
        Some(json!({"error":"unreadable"})),
        Some(json!([{"run_id":"broken"}])),
    ] {
        for sessions in [json!([]), json!([{"busy":false}])] {
            let mut s = snapshot();
            s["session_activity"]["sessions"] = sessions;
            if let Some(runs) = &runs {
                s["session_activity"]["runs"] = runs.clone();
            } else {
                s["session_activity"]
                    .as_object_mut()
                    .unwrap()
                    .remove("runs");
            }
            assert_eq!(line(&events(&s)), "FLOW-WATCHDOG: busy-quiet unknown=runs");
        }
    }
}

#[test]
fn missing_transcript_is_unknown() {
    let mut s = snapshot();
    s["session_activity"]["sessions"][0]
        .as_object_mut()
        .unwrap()
        .remove("transcript_mtime");
    assert!(line(&events(&s)).contains("unknown=transcript"));
}

#[test]
fn unreadable_session_list_or_clock_is_condition_local_unknown() {
    for (field, evidence) in [("sessions", Value::Null), ("now", json!("bad"))] {
        let mut s = snapshot();
        s["session_activity"][field] = evidence;
        assert!(line(&events(&s)).contains("unknown="));
    }
    let mut s = snapshot();
    s["session_activity"] = Value::Null;
    assert!(line(&events(&s)).contains("unknown="));
}

#[test]
fn unknown_evidence_recovers_to_a_real_alert_without_other_rule_changes() {
    let dir = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    s["session_activity"]["sessions"][0]["transcript_mtime"] = Value::Null;
    s["session_activity"]["runs"] = Value::Null;
    let first = pass(dir.path(), &s);
    assert_eq!(first.len(), 2);
    assert!(line(&first[..1]).contains("unknown=transcript,runs"));
    assert_eq!(line(&first[1..]), "FLOW-WATCHDOG: busy-quiet unknown=runs");
    assert_eq!(pass(dir.path(), &s), first);
    let found = pass(dir.path(), &snapshot());
    assert_eq!(found.len(), 3);
    assert!(line(&found[2..]).contains("quiet_age_seconds=1260"));
}

#[test]
fn invalid_timestamps_are_unknown() {
    for mtime in [json!(-1), json!(NOW + 1), json!(true)] {
        let mut s = snapshot();
        s["session_activity"]["sessions"][0]["transcript_mtime"] = mtime;
        assert!(line(&events(&s)).contains("unknown=transcript"));
    }
    for ended in [json!(-1), json!(NOW + 1), json!(true)] {
        let mut s = snapshot();
        s["session_activity"]["runs"][0]["ended"] = ended;
        let found = events(&s);
        assert!(line(&found[..1]).contains("unknown=runs"));
        assert_eq!(line(&found[1..]), "FLOW-WATCHDOG: busy-quiet unknown=runs");
    }
}

#[test]
fn a_second_session_gets_its_own_line_without_refiring_the_first() {
    let dir = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    let first = pass(dir.path(), &s);
    assert_eq!(first.len(), 1);
    let mut row = s["session_activity"]["sessions"][0].clone();
    row["session_id"] = "session-3".into();
    s["session_activity"]["sessions"]
        .as_array_mut()
        .unwrap()
        .push(row);
    let found = pass(dir.path(), &s);
    assert_eq!(found.len(), 2);
    assert_eq!(found[0], first[0]);
    assert!(line(&found[1..]).contains("session=\"session-3\""));
    assert_eq!(pass(dir.path(), &s), found);
}

#[test]
fn identifiers_are_escaped_and_duplicate_session_rows_emit_one_line() {
    let mut s = snapshot();
    s["session_activity"]["sessions"][0]["session_id"] = "session\n1".into();
    s["session_activity"]["sessions"][0]["role"] = "build\nseat".into();
    s["session_activity"]["runs"][1]["parent_session"] = "session\n1".into();
    s["session_activity"]["runs"][1]["run_id"] = "run\nlast".into();
    let row = s["session_activity"]["sessions"][0].clone();
    s["session_activity"]["sessions"]
        .as_array_mut()
        .unwrap()
        .push(row);
    let found = events(&s);
    let message = line(&found);
    for text in [
        "session=\"session\\n1\"",
        "role=\"build\\nseat\"",
        "last_ended_run=\"run\\nlast\"",
    ] {
        assert!(message.contains(text), "{message}");
    }
}

#[test]
fn absent_reader_preserves_old_snapshots() {
    let mut s = snapshot();
    s.as_object_mut().unwrap().remove("session_activity");
    assert!(events(&s).is_empty());
}
