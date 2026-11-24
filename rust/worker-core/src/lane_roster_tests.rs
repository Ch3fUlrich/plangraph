use super::*;
use std::fs;

fn write(path: &Path, text: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn native_run(state: &Path, id: &str, status: &str, prompt: &str, card: Value) {
    let dir = state.join("runs").join(id);
    write(
        &dir.join("record.json"),
        &serde_json::json!({"run_id":id,"run_kind":"native-codex","status":status}).to_string(),
    );
    write(
        &dir.join("native-request.json"),
        &serde_json::json!({"prompt":prompt,"card":card}).to_string(),
    );
}

#[test]
fn keys_come_from_item_lines_headings_and_trailers() {
    let text = "# Batch writer brief\n## plangraph:A-1 | first\nItem key: plangraph:A-2\n\
                Plan-Item: plangraph:A-3\nPlan-Item: <the key of the item>\n### notes | x\n";
    let keys: Vec<_> = item_keys(text).into_iter().collect();
    assert_eq!(keys, ["plangraph:A-1", "plangraph:A-2", "plangraph:A-3"]);
}

#[test]
fn a_versioned_batch_input_names_its_items_and_its_lane() {
    let input = include_str!("../../pg-spawnd/tests/fixtures/feeder-batch-input.txt");
    let keys: Vec<_> = item_keys(input).into_iter().collect();
    assert_eq!(keys, ["plangraph:ALPHA-1", "plangraph:ALPHA-2"]);
    assert!(brief_name(input).is_some_and(|name| name.starts_with("feeder-")));
    let crlf = input.replacen("V1\n", "V1\r\n", 1);
    assert_eq!(item_keys(&crlf).len(), 2);
}

#[test]
fn the_key_shape_is_the_native_run_record_rule() {
    for key in [
        "plangraph:D-5047",
        "plan.graph-1_test:D-5047.part_2:review",
        &format!("p:{}", "x".repeat(180)),
        &format!("{}:{}", "p".repeat(19), "x".repeat(180)),
    ] {
        assert!(valid_key(key), "{key}");
    }
    for key in [
        "garbage",
        ":x",
        "plangraph:",
        "",
        "bad/item",
        &format!("p:{}", "x".repeat(181)),
    ] {
        assert!(!valid_key(key), "{key}");
    }
}

#[test]
fn only_runs_that_still_own_their_worktree_are_lanes() {
    let dir = tempfile::tempdir().unwrap();
    let prompt = "Item key: plangraph:A-1\nNAME: batch9-x\n";
    let none = Value::Null;
    native_run(dir.path(), "run-live", "running", prompt, none.clone());
    native_run(dir.path(), "run-needs", "needs_input", prompt, none.clone());
    native_run(dir.path(), "run-done", "completed", prompt, none.clone());
    native_run(dir.path(), "run-nokeys", "running", "no items here", none);
    let lanes = native_lanes(dir.path());
    let names: Vec<_> = lanes.iter().map(|l| (&*l.name, &*l.detail)).collect();
    assert_eq!(names, [("batch9-x", "run-live"), ("batch9-x", "run-needs")]);
}

#[test]
fn a_native_batch_holds_every_persisted_item_key() {
    let dir = tempfile::tempdir().unwrap();
    let card = serde_json::json!({"item_key":"p:ONE","item_keys":["p:ONE","p:TWO","p:THREE"]});
    native_run(dir.path(), "run-batch", "running", "plain prompt", card);
    let lanes = native_lanes(dir.path());
    let keys: Vec<_> = lanes[0].keys.iter().map(String::as_str).collect();
    assert_eq!(keys, ["p:ONE", "p:THREE", "p:TWO"]);
}

fn claude_record(dir: &Path, session: &str, name: &str, sid: &str, brief: &Path) {
    let record = serde_json::json!({"name":name,"session_name":session,"session_id":sid,
        "prompt_file":brief,"ts":1});
    write(&dir.join(format!("{session}.json")), &record.to_string());
}

#[test]
fn a_claude_lane_joins_the_fleet_lifecycle_by_session_id() {
    let dir = tempfile::tempdir().unwrap();
    let batch = include_str!("../../pg-spawnd/tests/fixtures/feeder-batch-input.txt");
    let brief = dir.path().join("brief.md");
    write(&brief, batch);
    claude_record(dir.path(), "writer-0001", "writer", "sid-live", &brief);
    claude_record(dir.path(), "writer-0002", "writer", "sid-exited", &brief);
    claude_record(dir.path(), "review-0003", "review", "sid-live", &brief);
    write(&dir.path().join("writer-0001.ready.json"), "{}");
    let live: BTreeSet<String> = ["sid-live".to_owned()].into();
    // The record is a day and more old (ts 1): a running writer stays, an exited one drops.
    let lanes = claude_lanes(dir.path(), Some(&live));
    assert_eq!(lanes.len(), 1);
    assert_eq!(lanes[0].detail, "writer-0001");
    assert_eq!(lanes[0].keys.len(), 2, "the versioned prompt is decoded");
    // With the lifecycle unknown every unstopped writer record counts; never a non-writer.
    assert_eq!(claude_lanes(dir.path(), None).len(), 2);
}

#[test]
fn pull_requests_and_branches_hold_the_items_their_names_carry() {
    let json = r#"[{"number":12,"headRefName":"lane/b1","title":"b1",
        "body":"Plan-Item: plangraph:C-1\nPlan-Item: plangraph:C-2"},
        {"number":13,"headRefName":"lane/orch-live-lane","title":"none","body":""}]"#;
    let lanes = pull_request_lanes(json);
    assert_eq!(lanes.len(), 2);
    assert_eq!(lanes[0].describe(), "open pull request lane/b1 (#12)");
    assert!(lanes[0].holds("plangraph:C-2"));
    assert!(!lanes[0].holds("plangraph:C-3"));
    // The keeper's convention: a branch holds the item whose id (lower case) its name carries.
    assert!(lanes[1].holds("plangraph:ORCH-LIVE-LANE"));
    assert!(!lanes[1].holds("plangraph:OTHER"));
    let open = branch_lanes(["lane/x-9", ""]);
    assert_eq!(open.len(), 1);
    assert!(open[0].holds("plangraph:X-9"));
}

#[test]
fn a_reservation_holds_its_items_until_the_record_exists_or_it_ages_out() {
    let dir = tempfile::tempdir().unwrap();
    let keys: BTreeSet<String> = ["p:R-1".to_owned()].into();
    write_reservation(dir.path(), "run-r1", "lane-r", &keys, 1000).unwrap();
    assert_eq!(reserved_lanes(dir.path(), 1001).len(), 1);
    assert!(reserved_lanes(dir.path(), 1000 + RESERVATION_TTL_S).is_empty());
    native_run(
        dir.path(),
        "run-r1",
        "running",
        "Item key: p:R-1\n",
        Value::Null,
    );
    assert!(
        reserved_lanes(dir.path(), 1001).is_empty(),
        "the record is the lane now"
    );
    assert!(write_reservation(dir.path(), "bad id", "n", &keys, 1).is_err());
}
