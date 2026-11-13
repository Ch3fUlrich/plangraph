use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};

#[path = "flow_watchdog_quiet/runs.rs"]
mod runs;

const NOW: u64 = 10_000;
const NO_ENDED_RUN: &str =
    "session=\"session-1\" role=\"build\" quiet_age_seconds=1260 last_ended_run=none";
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
    Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .args([input.as_path(), dir])
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
    let dir = tempfile::tempdir().unwrap();
    pass(dir.path(), snapshot)
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
fn exact(events: &[Value], detail: &str) {
    assert_eq!(line(events), format!("FLOW-WATCHDOG: busy-quiet {detail}"));
}
fn active(dir: &Path) -> String {
    fs::read_to_string(dir.join("active")).unwrap_or_default()
}
#[test]
fn busy_session_with_readable_empty_runs_alerts_without_a_last_ended_run() {
    let mut s = snapshot();
    s["session_activity"]["runs"] = json!([]);
    exact(&events(&s), NO_ENDED_RUN);
}
#[test]
fn busy_session_quiet_for_nineteen_minutes_with_all_runs_ended_does_not_alert() {
    let mut s = snapshot();
    rows(&mut s, "sessions")[0]["transcript_mtime"] = (NOW - 19 * 60).into();
    s["session_activity"]["runs"][2]["ended"] = 9500.into();
    assert_eq!(events(&s), Vec::<Value>::new());
}
#[test]
fn busy_session_with_only_other_sessions_runs_alerts_without_a_last_ended_run() {
    let mut s = snapshot();
    rows(&mut s, "runs").retain(|run| run["parent_session"] != "session-1");
    exact(&events(&s), NO_ENDED_RUN);
}
#[test]
fn present_non_object_activity_reports_unknown_reader() {
    for block in [Value::Null, json!([]), json!("bad"), json!(17)] {
        for supported in [false, true] {
            let mut s = snapshot();
            s["session_activity_supported"] = supported.into();
            s["session_activity"] = block.clone();
            exact(&events(&s), "unknown=reader");
        }
    }
}
#[test]
fn idle_session_quiet_for_over_twenty_minutes_never_alerts_from_a_clean_state() {
    let mut s = snapshot();
    s["session_activity"]["sessions"][0]["busy"] = false.into();
    assert_eq!(events(&s), Vec::<Value>::new());
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
        let before = match already_alerted {
            true => pass(dir.path(), &s),
            false => vec![],
        };
        if live_run {
            s["session_activity"]["runs"][1]["ended"] = Value::Null;
        } else {
            s["session_activity"]["sessions"][0]["busy"] = false.into();
        }
        assert_eq!(pass(dir.path(), &s), before);
        assert_eq!(active(dir.path()), "");
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
    let cases = json!([null, {"error":"unreadable"}, "bad", -1, NOW + 1, true]);
    for evidence in std::iter::once(None).chain(cases.as_array().unwrap().iter().map(Some)) {
        let dir = tempfile::tempdir().unwrap();
        let mut s = snapshot();
        let session = &mut rows(&mut s, "sessions")[0];
        if let Some(evidence) = evidence {
            session["transcript_mtime"] = evidence.clone();
        } else {
            session.as_object_mut().unwrap().remove("transcript_mtime");
        }
        s["host_drift"] = true.into();
        let found = pass(dir.path(), &s);
        assert_eq!(found.len(), 2, "{found:?}"); // One quiet and one unrelated event.
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
fn unreadable_session_list_or_clock_is_condition_local_unknown() {
    let cases = [
        ("sessions", "sessions", Value::Null),
        ("now", "clock", json!("bad")),
    ];
    for (field, reason, evidence) in cases {
        let mut s = snapshot();
        s["session_activity"][field] = evidence;
        exact(&events(&s), &format!("unknown={reason}"));
    }
    let mut s = snapshot();
    s["session_activity"] = Value::Null;
    exact(&events(&s), "unknown=reader");
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
    let quiet = "role=\"build\" quiet_age_seconds=1260 last_ended_run=\"run-last\"";
    for field in ["collector_note", "busy", "role"] {
        let detail = if field == "collector_note" {
            quiet
        } else {
            "unknown=sessions"
        };
        let dir = tempfile::tempdir().unwrap();
        let mut s = snapshot();
        s["session_activity"]["sessions"][0][field] = Value::Null;
        let first = pass(dir.path(), &s);
        exact(&first, &format!("session=\"session-1\" {detail}"));
        // add_session clones row 0, so session-3 inherits the nulled field.
        add_session(&mut s, "session-3");
        let found = pass(dir.path(), &s);
        assert_eq!(found.len(), 2);
        assert_eq!(found[0], first[0]);
        let no_runs = NO_ENDED_RUN.split_once(' ').unwrap().1;
        let detail = if field == "collector_note" {
            no_runs
        } else {
            detail
        };
        exact(&found[1..], &format!("session=\"session-3\" {detail}"));
        if field != "collector_note" {
            let identity = "busy-quiet session=\"session-3\" unknown=sessions";
            assert_eq!(found[1]["active"], identity);
        }
        rows(&mut s, "sessions")
            .iter_mut()
            .for_each(|row| row[field] = json!(17));
        assert_eq!(pass(dir.path(), &s), found);
    }
}
#[test]
fn identifiers_are_escaped_and_duplicate_session_rows_emit_one_line() {
    let mut s = snapshot();
    rows(&mut s, "sessions")[0]["session_id"] = "session\n1".into();
    rows(&mut s, "sessions")[0]["role"] = "build\nseat".into();
    rows(&mut s, "runs")[1]["parent_session"] = "session\n1".into();
    rows(&mut s, "runs")[1]["run_id"] = "run\nlast".into();
    add_session(&mut s, "session\n1");
    for unread in [Value::Null, json!("bad"), json!({"extra":17})] {
        s["session_activity"]["sessions"][1]["collector_note"] = unread;
        for _ in 0..2 {
            let found = events(&s);
            let message = line(&found);
            for text in [
                "session=\"session\\n1\"",
                "role=\"build\\nseat\"",
                "last_ended_run=\"run\\nlast\"",
            ] {
                assert!(message.contains(text), "{message}");
            }
            rows(&mut s, "sessions").reverse();
        }
    }
}
#[test]
fn absent_reader_preserves_old_snapshots() {
    let mut s = snapshot();
    s.as_object_mut().unwrap().remove("session_activity");
    assert!(events(&s).is_empty());
}
#[test]
fn capable_collectors_cannot_silently_omit_the_reader() {
    let mut s = snapshot();
    s["session_activity_supported"] = true.into();
    assert!(line(&events(&s)).contains("quiet_age_seconds=1260 "));
    s.as_object_mut().unwrap().remove("session_activity");
    exact(&events(&s), "unknown=reader");
    s["session_activity_supported"] = false.into();
    assert!(events(&s).is_empty());
    s["session_activity"] = snapshot()["session_activity"].clone();
    assert!(line(&events(&s)).contains("quiet_age_seconds=1260 "));
}
#[test]
fn invalid_snapshots_preserve_quiet_identity_until_an_observed_recovery() {
    for (field, invalid) in [("pace", Value::Null), ("budget", json!(0))] {
        let dir = tempfile::tempdir().unwrap();
        let mut s = snapshot();
        let first = pass(dir.path(), &s);
        assert!(line(&first).contains("quiet_age_seconds=1260 "));
        s[field] = invalid;
        assert!(!invoke(dir.path(), &s).status.success());
        let identity = first[0]["active"].as_str().unwrap();
        assert!(active(dir.path()).lines().any(|line| line == identity));
        let found = pass(dir.path(), &snapshot());
        assert_eq!(found.len(), 2, "{found:?}");
        assert_eq!(found[0], first[0]);
        assert_eq!(message(&found[1]), "FLOW-WATCHDOG: invalid snapshot");
        let mut recovered = snapshot();
        recovered["session_activity"]["sessions"][0]["busy"] = false.into();
        assert_eq!(pass(dir.path(), &recovered), found);
        let next = pass(dir.path(), &snapshot());
        assert_eq!(next.len(), 3);
        assert_eq!(next[2], first[0]);
    }
}
#[test]
fn outer_deserialization_and_validation_errors_supersede_quiet_evidence() {
    let cases =
        json!({"pace":null,"hosts":[{"host":"broken"}],"infra_failed":1,"ci_failed":1,"budget":0});
    for (field, invalid) in cases.as_object().unwrap() {
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
