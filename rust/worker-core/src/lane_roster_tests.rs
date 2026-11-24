use super::*;
use std::fs;

fn write(path: &Path, text: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn native_run(state: &Path, id: &str, status: &str, prompt: &str) {
    let dir = state.join("runs").join(id);
    write(
        &dir.join("record.json"),
        &serde_json::json!({"run_id":id,"run_kind":"native-codex","status":status}).to_string(),
    );
    write(
        &dir.join("native-request.json"),
        &serde_json::json!({"prompt":prompt,"card":{}}).to_string(),
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
fn only_runs_that_still_own_their_worktree_are_lanes() {
    let dir = tempfile::tempdir().unwrap();
    let prompt = "Item key: plangraph:A-1\nNAME: batch9-x\n";
    native_run(dir.path(), "run-live", "running", prompt);
    native_run(dir.path(), "run-needs", "needs_input", prompt);
    native_run(dir.path(), "run-done", "completed", prompt);
    native_run(dir.path(), "run-nokeys", "running", "no items here");
    let lanes = native_lanes(dir.path());
    let names: Vec<_> = lanes.iter().map(|l| (&*l.name, &*l.detail)).collect();
    assert_eq!(names, [("batch9-x", "run-live"), ("batch9-x", "run-needs")]);
}

#[test]
fn a_claude_lane_needs_a_running_unstopped_record_with_a_readable_brief() {
    let dir = tempfile::tempdir().unwrap();
    let brief = dir.path().join("brief.md");
    write(&brief, "Item key: plangraph:B-1\nNAME: batch7\n");
    let record = |name: &str, extra: serde_json::Value| {
        let mut r = serde_json::json!({"name":name,"prompt_file":brief});
        r.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        write(&dir.path().join(format!("{name}.json")), &r.to_string());
    };
    record("writer-0001", serde_json::json!({"state":"running"}));
    record("writer-0002", serde_json::json!({"stopped_ts": 5}));
    record("writer-0003", serde_json::json!({"state":"stopped"}));
    record("writer-0004", serde_json::json!({"ts": 100}));
    write(&dir.path().join("writer-0001.ready.json"), "{}");
    let lanes = claude_lanes(dir.path(), 100 + CLAUDE_LANE_MAX_AGE_S + 1);
    assert_eq!(lanes.len(), 1, "a record past the age limit is not live");
    assert_eq!(lanes[0].detail, "writer-0001");
    assert_eq!(lanes[0].name, "batch7");
    assert_eq!(lanes[0].kind, LaneKind::Claude);
}

#[test]
fn open_pull_requests_are_lanes_by_their_plan_item_trailers() {
    let json = r#"[{"number":12,"headRefName":"lane/b1","title":"b1",
        "body":"Plan-Item: plangraph:C-1\nPlan-Item: plangraph:C-2"},
        {"number":13,"headRefName":"lane/b2","title":"none","body":""}]"#;
    let lanes = pull_request_lanes(json);
    assert_eq!(lanes.len(), 1);
    assert_eq!(lanes[0].describe(), "open pull request lane/b1 (#12)");
    assert_eq!(lanes[0].keys.len(), 2);
}
