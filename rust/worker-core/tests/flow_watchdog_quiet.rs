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
            "runs":[{"run_id":"run-last","parent_session":"session-1","ended":9000},
                {"run_id":"run-first","parent_session":"session-1","ended":8800},
                {"run_id":"run-other","parent_session":"session-2","ended":null}]}})
}

fn pass(dir: &Path, snapshot: &Value) -> Vec<Value> {
    let input = dir.join("input.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .arg(input)
        .arg(dir)
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
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
    let found = events(&snapshot());
    assert_eq!(line(&found), "FLOW-WATCHDOG: busy-quiet session=\"session-1\" role=\"build\" quiet_age_seconds=1260 last_ended_run=\"run-last\"");
}

#[test]
fn one_own_live_run_prevents_alert() {
    let mut s = snapshot();
    s["session_activity"]["runs"][1]["ended"] = Value::Null;
    assert!(events(&s).is_empty());
}

#[test]
fn quiet_19_minutes_and_exactly_20_minutes_do_not_alert() {
    for seconds in [19 * 60, 20 * 60, 20 * 60 + 1] {
        let mut s = snapshot();
        s["session_activity"]["sessions"][0]["transcript_mtime"] = (NOW - seconds).into();
        assert_eq!(events(&s).len(), usize::from(seconds > 20 * 60));
    }
}

#[test]
fn idle_session_does_not_alert() {
    let mut s = snapshot();
    s["session_activity"]["sessions"][0]["busy"] = false.into();
    assert!(events(&s).is_empty());
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
    s["session_activity"]["runs"][0]["run_id"] = "run-renamed".into();
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
        assert_eq!(found.len(), 2);
        assert_eq!(found[0]["message"], "FLOW-WATCHDOG: host-drift");
        assert!(
            line(&found[1..]).contains("session=\"session-1\" role=\"build\" unknown=transcript")
        );
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
        assert!(line(&events(&s)).contains("unknown=runs"));
    }
}

#[test]
fn missing_transcript_and_missing_run_reader_are_unknown() {
    for field in ["transcript_mtime", "runs"] {
        let mut s = snapshot();
        if field == "runs" {
            s["session_activity"].as_object_mut().unwrap().remove(field);
        } else {
            s["session_activity"]["sessions"][0]
                .as_object_mut()
                .unwrap()
                .remove(field);
        }
        assert!(line(&events(&s)).contains("unknown="));
    }
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
    assert!(line(&pass(dir.path(), &s)).contains("unknown=transcript,runs"));
    let found = pass(dir.path(), &snapshot());
    assert_eq!(found.len(), 2);
    assert!(line(&found[1..]).contains("quiet_age_seconds=1260"));
}

#[test]
fn invalid_timestamps_are_unknown_and_fractional_seconds_keep_strict_threshold() {
    for mtime in [json!(-1), json!(NOW + 1), json!(true)] {
        let mut s = snapshot();
        s["session_activity"]["sessions"][0]["transcript_mtime"] = mtime;
        assert!(line(&events(&s)).contains("unknown=transcript"));
    }
    for ended in [json!(-1), json!(NOW + 1), json!(true)] {
        let mut s = snapshot();
        s["session_activity"]["runs"][0]["ended"] = ended;
        assert!(line(&events(&s)).contains("unknown=runs"));
    }
    let mut s = snapshot();
    s["session_activity"]["sessions"][0]["transcript_mtime"] = json!(NOW as f64 - 1200.5);
    assert!(line(&events(&s)).contains("quiet_age_seconds=1200"));
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
    s["session_activity"]["runs"][0]["parent_session"] = "session\n1".into();
    s["session_activity"]["runs"][0]["run_id"] = "run\nlast".into();
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
