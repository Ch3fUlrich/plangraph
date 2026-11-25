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
        "prompt_file":brief,"ts":1,"host":"here"});
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
    let lanes = claude_lanes(dir.path(), &|_| Some(live.clone()), "here");
    assert_eq!(lanes.len(), 1);
    assert_eq!(lanes[0].detail, "writer-0001");
    assert_eq!(lanes[0].keys.len(), 2, "the versioned prompt is decoded");
    // With the lifecycle unknown every unstopped writer record counts; never a non-writer.
    assert_eq!(claude_lanes(dir.path(), &|_| None, "here").len(), 2);
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

fn writer_on(dir: &Path, session: &str, sid: &str, host: &str, ssh: Option<&str>) {
    let brief = dir.join("brief.md");
    write(&brief, "Item key: p:REMOTE\nNAME: far-lane\n");
    let mut record = serde_json::json!({"name":"writer","session_name":session,"session_id":sid,
        "prompt_file":brief,"host":host});
    if let Some(ssh) = ssh {
        record["ssh"] = serde_json::json!(ssh);
    }
    write(&dir.join(format!("{session}.json")), &record.to_string());
}

#[test]
fn a_remote_writer_joins_its_own_hosts_lifecycle_and_a_crashed_session_does_not_count() {
    let dir = tempfile::tempdir().unwrap();
    writer_on(
        dir.path(),
        "writer-0001",
        "sid-far",
        "elsewhere",
        Some("far-alias"),
    );
    writer_on(
        dir.path(),
        "writer-0002",
        "sid-gone",
        "elsewhere",
        Some("far-alias"),
    );
    writer_on(dir.path(), "writer-0003", "sid-nossh", "elsewhere", None);
    let polled = std::cell::RefCell::new(Vec::new());
    let lifecycle = |ssh: Option<&str>| {
        polled.borrow_mut().push(ssh.map(str::to_owned));
        // `claude agents` here lists nothing; the far host lists sid-far only.
        Some(ssh.map_or_else(BTreeSet::new, |_| ["sid-far".to_owned()].into()))
    };
    let lanes = claude_lanes(dir.path(), &lifecycle, "here");
    let sessions: Vec<_> = lanes.iter().map(|l| l.detail.as_str()).collect();
    // sid-gone is over on its own host; sid-nossh has no destination to ask, so it stays.
    assert_eq!(sessions, ["writer-0001", "writer-0003"]);
    assert_eq!(
        *polled.borrow(),
        [Some("far-alias".to_owned())],
        "one poll per host"
    );
    // A lifecycle that cannot be read keeps every unstopped record.
    assert_eq!(claude_lanes(dir.path(), &|_| None, "here").len(), 3);
    assert!(FINISHED.contains(&"crashed") && FINISHED.contains(&"cancelled"));
}

#[test]
fn a_writer_recorded_under_the_fleet_alias_of_this_host_is_local() {
    let dir = tempfile::tempdir().unwrap();
    // The launcher records the policy alias; the OS hostname differs.
    let alias = local_alias();
    let own = format!("{}.lan", &alias[..4]);
    writer_on(
        dir.path(),
        "writer-0001",
        "sid-dead",
        &alias,
        Some("ignored"),
    );
    let lifecycle = |ssh: Option<&str>| {
        assert!(ssh.is_none(), "a local record is polled locally");
        Some(BTreeSet::new())
    };
    assert!(claude_lanes(dir.path(), &lifecycle, &own).is_empty());
    assert!(is_local_host(&alias, &alias));
    assert!(is_local_host(&alias, &own));
    assert!(!is_local_host(&alias, "other"));
    assert!(!is_local_host("far", "here"));
}

#[test]
fn a_bad_primary_item_never_falls_through_to_the_alias() {
    let by = |card: Value, env: Option<&str>| -> Vec<String> {
        card_keys(&card, env).into_iter().collect()
    };
    // The run record discards an invalid explicit key (item_key stays null); so does admission.
    assert!(by(
        serde_json::json!({"item_key":5,"item":"p:ALIAS"}),
        Some("p:ENV")
    )
    .is_empty());
    assert!(by(
        serde_json::json!({"item_key":"bad key","item":"p:ALIAS"}),
        None
    )
    .is_empty());
    assert!(by(serde_json::json!({"item":["x"]}), Some("p:ENV")).is_empty());
    assert_eq!(
        by(serde_json::json!({"item_key":"","item":"p:ALIAS"}), None),
        ["p:ALIAS"]
    );
    assert_eq!(
        by(serde_json::json!({"item_key":null}), Some("bad")),
        Vec::<String>::new()
    );
}

#[test]
fn a_reservation_dropped_for_a_complete_record_cannot_open_a_gap_between_the_two_scans() {
    let dir = tempfile::tempdir().unwrap();
    let keys: BTreeSet<String> = ["p:ONE".to_owned(), "p:TWO".to_owned()].into();
    write_reservation(dir.path(), "run-g", "lane-g", &keys, 1000).unwrap();
    let run = dir.path().join("runs/run-g");
    let partial = serde_json::json!({"run_id":"run-g","run_kind":"native-codex",
        "status":"running","item_key":"p:ONE"});
    write(&run.join("record.json"), &partial.to_string());
    let sessions = dir.path().join("sessions");
    // The record completes after the reservation scan kept the reservation (the record was
    // partial) and before the run scan: the run scan must carry the second item.
    let lanes = local_lanes_between(dir.path(), &sessions, &|_| None, 1001, || {
        let complete = serde_json::json!({"run_id":"run-g","run_kind":"native-codex",
            "status":"running","item_key":"p:ONE","item_keys":["p:ONE","p:TWO"]});
        write(&run.join("record.json"), &complete.to_string());
    });
    for key in ["p:ONE", "p:TWO"] {
        assert!(lanes.iter().any(|lane| lane.holds(key)), "{key} is held");
    }
}

#[test]
fn a_deferred_key_the_table_lists_is_not_held_when_go_keys_leaves_it_out() {
    let body =
        "| PR | Plan-Item | Head |\n|---|---|---|\n| #1 | demo:DONE, demo:DEFERRED | `a` |\n\n\
                GO-KEYS [demo:DONE]; GO-DEFERRED [demo:DEFERRED]\n";
    let keys: Vec<_> = item_keys(body).into_iter().collect();
    assert_eq!(keys, ["demo:DONE"]);
    // Without a GO-KEYS block the table is the list.
    let table = "| #1 | demo:A, demo:B | `a` |\n";
    assert_eq!(item_keys(table).len(), 2);
}

#[test]
fn a_card_item_resolves_like_the_run_record_including_the_alias_and_the_environment() {
    let by = |card: Value, env: Option<&str>| -> Vec<String> {
        card_keys(&card, env).into_iter().collect()
    };
    assert_eq!(by(serde_json::json!({"item":"p:ALIAS"}), None), ["p:ALIAS"]);
    assert_eq!(
        by(serde_json::json!({"item_key":"p:A","item":"p:B"}), None),
        ["p:A"]
    );
    assert_eq!(by(serde_json::json!({}), Some("p:ENV")), ["p:ENV"]);
    assert_eq!(
        by(serde_json::json!({"item_key":"p:p:DUP"}), None),
        ["p:DUP"]
    );
    assert_eq!(
        by(
            serde_json::json!({"item_key":"p:A","item_keys":["p:B","not a key"]}),
            Some("p:ENV")
        ),
        ["p:A", "p:B"]
    );
}

#[test]
fn a_record_published_before_its_batch_keeps_the_reservation() {
    let dir = tempfile::tempdir().unwrap();
    let keys: BTreeSet<String> = ["p:ONE".to_owned(), "p:TWO".to_owned()].into();
    write_reservation(dir.path(), "run-b", "lane-b", &keys, 1000).unwrap();
    // The record names only the first item and its request is not written yet.
    let run = dir.path().join("runs/run-b");
    let record = serde_json::json!({"run_id":"run-b","run_kind":"native-codex","status":"running",
        "item_key":"p:ONE"});
    write(&run.join("record.json"), &record.to_string());
    assert_eq!(
        reserved_lanes(dir.path(), 1001).len(),
        1,
        "the second item is still reserved"
    );
    let both = serde_json::json!({"run_id":"run-b","run_kind":"native-codex","status":"running",
        "item_key":"p:ONE","item_keys":["p:ONE","p:TWO"]});
    write(&run.join("record.json"), &both.to_string());
    assert!(reserved_lanes(dir.path(), 1001).is_empty());
    let lanes = native_lanes(dir.path());
    assert!(
        lanes[0].holds("p:TWO"),
        "a mirrored record's secondary item is held"
    );
}

#[test]
fn a_generated_batch_pull_request_body_names_every_item() {
    let body = include_str!("../tests/fixtures/batch-pr-body.md");
    let lanes = pull_request_lanes(
        &serde_json::json!([{"number":5,"headRefName":"merge/train-9","title":"Train","body":body}])
            .to_string(),
    );
    for key in ["demo:ITEM-9", "demo:ITEM-10", "demo:KEYS-ONLY"] {
        assert!(lanes[0].holds(key), "{key}");
    }
    assert!(
        !lanes[0].holds("demo:DEFERRED"),
        "a deferred key is not delivered by this pull request"
    );
}
