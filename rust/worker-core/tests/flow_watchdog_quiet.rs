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

fn rows<'a>(s: &'a mut Value, field: &str) -> &'a mut Vec<Value> {
    s["session_activity"][field].as_array_mut().unwrap()
}

fn add_session(s: &mut Value, id: &str) {
    let mut row = s["session_activity"]["sessions"][0].clone();
    row["session_id"] = id.into();
    rows(s, "sessions").push(row);
}

fn message(event: &Value) -> &str {
    event["message"].as_str().unwrap()
}

fn invoke(dir: &Path, snapshot: &Value) -> std::process::Output {
    let input = dir.join("input.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    // main() takes SNAPSHOT.json then STATE_DIR; no environment is required.
    Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .arg(input)
        .arg(dir)
        .env_clear()
        .output()
        .unwrap()
}

fn pass(dir: &Path, snapshot: &Value) -> Vec<Value> {
    let output = invoke(dir, snapshot);
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
    let line = message(&events[0]);
    assert_eq!(line.lines().count(), 1);
    assert!(line.starts_with("FLOW-WATCHDOG: busy-quiet "), "{line}");
    line
}

#[test]
fn latest_ended_run_is_own_and_ties_choose_greatest_id_in_either_order() {
    for (first, last, other) in [(8800, 9000, 9500), (9000, 9000, NOW), (8600, 8800, 9500)] {
        let mut s = snapshot();
        for (run, ended) in rows(&mut s, "runs").iter_mut().zip([first, last, other]) {
            run["ended"] = ended.into();
        }
        for _ in 0..2 {
            assert_eq!(line(&events(&s)), "FLOW-WATCHDOG: busy-quiet session=\"session-1\" role=\"build\" quiet_age_seconds=1260 last_ended_run=\"run-last\"");
            rows(&mut s, "runs").reverse();
        }
    }
}

#[test]
fn idle_or_live_own_run_clears_identity_and_recovery_allows_first_or_repeat_alert() {
    for (already_alerted, live_run) in [(false, true), (true, true), (true, false)] {
        let dir = tempfile::tempdir().unwrap();
        let mut s = snapshot();
        let before = if already_alerted {
            pass(dir.path(), &s)
        } else {
            vec![]
        };
        if live_run {
            s["session_activity"]["runs"][1]["ended"] = Value::Null;
        } else {
            s["session_activity"]["sessions"][0]["busy"] = false.into();
        }
        assert_eq!(pass(dir.path(), &s), before);
        assert_eq!(
            fs::read_to_string(dir.path().join("active")).unwrap_or_default(),
            ""
        );
        let found = pass(dir.path(), &snapshot());
        assert_eq!(found.len(), before.len() + 1);
        assert!(line(&found[before.len()..]).ends_with("last_ended_run=\"run-last\""));
        if already_alerted {
            assert_eq!(found[0], found[1]); // Unchanged quiet period can re-alert after recovery.
        }
        assert_eq!(pass(dir.path(), &snapshot()), found);
    }
}

#[test]
fn quiet_threshold_has_negative_and_positive_controls() {
    let mut s = snapshot();
    for seconds in [1140.0, 1200.0, 1201.0, 1200.5, 1200.001] {
        s["session_activity"]["sessions"][0]["transcript_mtime"] = (NOW as f64 - seconds).into();
        let found = events(&s);
        if seconds <= 1200.0 {
            assert!(found.is_empty(), "{found:?}");
        } else {
            assert!(line(&found).contains("quiet_age_seconds=1201 "));
        }
    }
}

#[test]
fn known_idle_rows_skip_missing_empty_or_malformed_session_details() {
    // docs/flow-watchdog.md and the round-1 changelog: skip known idle rows
    // before evidence validation; unreadable transcript is unknown for busy rows.
    for row in [
        json!({"busy":false,"session_id":"session-1","role":"build","transcript_mtime":8740}),
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
    assert!(message(&next[1]).contains("quiet_age_seconds=1261"));
}

#[test]
fn unreadable_transcript_is_unknown_in_its_own_deduplicated_line() {
    for evidence in [
        None,
        Some(Value::Null),
        Some(json!({"error":"unreadable"})),
        Some(json!("bad")),
        Some(json!(-1)),
        Some(json!(NOW + 1)),
        Some(json!(true)),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let mut s = snapshot();
        if let Some(evidence) = evidence {
            s["session_activity"]["sessions"][0]["transcript_mtime"] = evidence;
        } else {
            s["session_activity"]["sessions"][0]
                .as_object_mut()
                .unwrap()
                .remove("transcript_mtime");
        }
        s["host_drift"] = true.into();
        let found = pass(dir.path(), &s);
        assert!(found
            .iter()
            .map(message)
            .any(|text| text.contains("host-drift")));
        let quiet: Vec<_> = found
            .iter()
            .filter(|event| message(event).starts_with("FLOW-WATCHDOG: busy-quiet "))
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
        json!([{"run_id":"broken","ended":9000}]),
        json!([{"run_id":"broken","parent_session":"","ended":9000}]),
        json!([{"run_id":"broken","parent_session":null,"ended":9000}]),
        json!([{"run_id":"broken","parent_session":17,"ended":9000}]),
    ] {
        let mut s = snapshot();
        add_session(&mut s, "session-2");
        s["session_activity"]["runs"] = runs;
        let found = events(&s);
        assert_eq!(found.len(), 2, "{found:?}");
        for (event, id) in found.iter().zip(["session-1", "session-2"]) {
            assert_eq!(
                line(std::slice::from_ref(event)),
                format!("FLOW-WATCHDOG: busy-quiet session={id:?} role=\"build\" unknown=runs")
            );
        }
    }
}

#[test]
fn malformed_runs_only_invalidate_their_known_parent() {
    for record in [
        json!({"ended":9000}),
        json!({"run_id":"","ended":9000}),
        json!({"run_id":17,"ended":9000}),
        json!({"run_id":"broken"}),
        json!({"run_id":"broken","ended":"bad"}),
        json!({"run_id":"broken","ended":NOW + 1}),
        json!({"run_id":"broken","ended":-1}),
        json!({"run_id":"broken","ended":true}),
    ] {
        for parent in ["session-1", "session-2"] {
            let mut s = snapshot();
            add_session(&mut s, "session-2");
            s["session_activity"]["runs"][2]["ended"] = 9500.into();
            let mut record = record.clone();
            record["parent_session"] = parent.into();
            rows(&mut s, "runs").push(record);
            let found = events(&s);
            assert_eq!(found.len(), 2, "{found:?}");
            let affected = found
                .iter()
                .find(|e| message(e).contains(&format!("session={parent:?} ")))
                .unwrap();
            assert!(message(affected).ends_with("unknown=runs"), "{found:?}");
            let healthy = found
                .iter()
                .map(message)
                .find(|text| text.contains("quiet_age_seconds=1260 "));
            assert!(healthy.is_some(), "{found:?}");
        }
    }
}

#[test]
fn missing_or_unreadable_run_block_is_unknown_even_without_busy_sessions() {
    for runs in [
        None,
        Some(Value::Null),
        Some(json!({"error":"unreadable"})),
        Some(json!([{"run_id":"broken"}])),
        Some(json!([{"parent_session":"session-1","ended":"bad"}])),
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
    assert!(line(&first).contains("unknown=transcript,runs"));
    assert_eq!(pass(dir.path(), &s), first);
    let found = pass(dir.path(), &snapshot());
    assert_eq!(found.len(), 2);
    assert!(line(&found[1..]).contains("quiet_age_seconds=1260"));
}

#[test]
fn a_second_session_gets_its_own_line_without_refiring_the_first() {
    let dir = tempfile::tempdir().unwrap();
    let mut s = snapshot();
    let first = pass(dir.path(), &s);
    assert_eq!(first.len(), 1);
    add_session(&mut s, "session-3");
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
    add_session(&mut s, "session\n1");
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

#[test]
fn outer_deserialization_and_validation_errors_supersede_quiet_evidence() {
    for (field, invalid) in [
        ("pace", Value::Null),
        ("hosts", json!([{"host":"broken"}])),
        ("infra_failed", json!(1)),
        ("ci_failed", json!(1)),
        ("budget", json!(0)),
    ] {
        for activity in [snapshot()["session_activity"].clone(), Value::Null] {
            let dir = tempfile::tempdir().unwrap();
            let mut s = snapshot();
            s[field] = invalid.clone();
            s["session_activity"] = activity;
            let output = invoke(dir.path(), &s);
            assert!(!output.status.success(), "{output:?}");
            let stderr = String::from_utf8(output.stderr).unwrap();
            assert!(stderr.contains("invalid snapshot"), "{stderr}");
            let found: Vec<Value> =
                serde_json::from_slice(&fs::read(dir.path().join("queue.json")).unwrap()).unwrap();
            assert_eq!(found.len(), 1, "{found:?}");
            assert_eq!(found[0]["message"], "FLOW-WATCHDOG: invalid snapshot");
        }
    }
}
