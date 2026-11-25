use super::*;
use serde_json::json;

struct Fixed(Vec<Lane>);
impl LaneSource for Fixed {
    fn lanes(&self, _: &[String]) -> Vec<Lane> {
        self.0.clone()
    }
}

fn held(key: &str) -> Fixed {
    Fixed(vec![Lane {
        name: "writer-0673".into(),
        keys: vec![key.into()],
        source: "run",
    }])
}

fn write_run(state: &Path, id: &str, record: &Value) {
    let run = state.join("runs").join(id);
    fs::create_dir_all(&run).unwrap();
    fs::write(run.join("record.json"), record.to_string()).unwrap();
}

fn request<'a>(state: &'a Path, extra: &'a [String]) -> LaunchRequest<'a> {
    LaunchRequest {
        prompt: "Brief\n### demo:ALPHA | title\nWhy: x\n",
        extra,
        state,
    }
}

fn error_of(result: Result<Vec<String>, Refusal>) -> Refusal {
    result.unwrap_err()
}

const BRIEF: &str = "### demo:ALPHA | title\nWhy: x\n\n### demo:BETA | title\nWhy: y\n";

#[test]
fn refuses_a_key_of_another_live_lane_and_names_the_lane() {
    let error = admit(BRIEF, &[], &held("demo:BETA")).unwrap_err();
    assert_eq!(error.code, "lane_collision");
    assert!(error
        .message
        .starts_with("lane_collision: demo:BETA is held by live lane writer-0673"));
    assert!(!error.message.contains("demo:ALPHA"));
}

#[test]
fn a_free_key_passes_and_the_admitted_keys_are_returned() {
    assert_eq!(
        admit(BRIEF, &["demo:demo:EXTRA".into()], &held("demo:OTHER")).unwrap(),
        ["demo:ALPHA", "demo:BETA", "demo:EXTRA"]
    );
}

#[test]
fn live_runs_hold_their_item_key_brief_keys_and_recorded_items() {
    let runs = [
        (
            "run-1",
            json!({"run_id":"run-1","role":"implement","item_key":"demo:ONE"}),
        ),
        (
            "run-2",
            json!({"run_id":"run-2","role":"implement",
                   "manifest":{"brief":"### demo:TWO | t\nWhy\n"}}),
        ),
        (
            "run-3",
            json!({"run_id":"run-3","role":"implement","fleet_session_name":"writer-0009",
                   "item_keys":["demo:THREE"]}),
        ),
        (
            "run-4",
            json!({"run_id":"run-4","role":"implement","item_key":"bad key"}),
        ),
        (
            "run-5",
            json!({"run_id":"run-5","role":"review","item_key":"demo:ONE"}),
        ),
    ]
    .map(|(name, record)| (name.to_owned(), record));
    let by: Vec<_> = run_lanes(&runs)
        .into_iter()
        .map(|lane| (lane.name, lane.keys))
        .collect();
    assert_eq!(
        by,
        [
            ("run-1".to_string(), vec!["demo:ONE".to_string()]),
            ("run-2".to_string(), vec!["demo:TWO".to_string()]),
            ("writer-0009".to_string(), vec!["demo:THREE".to_string()]),
        ]
    );
}

#[test]
fn a_live_writer_without_fleet_name_or_run_id_still_holds_its_keys_under_its_directory_name() {
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-anon",
        &json!({"status":"running","role":"implement","item_key":"demo:ANON"}),
    );
    let error = error_of(admit_launch(&LaunchRequest {
        prompt: "Item key: demo:ANON\n",
        extra: &[],
        state: state.path(),
    }));
    assert!(
        error
            .message
            .contains("demo:ANON is held by live lane run-anon"),
        "{}",
        error.message
    );
}

#[test]
fn ownership_follows_the_shared_run_status_rule() {
    let state = tempfile::tempdir().unwrap();
    let status = |status: &str| json!({"status":status,"role":"implement","item_key":"demo:K"});
    let released = [
        "completed",
        "failed",
        "cancelled",
        "refused",
        "stalled",
        "timeout",
        "dry_run",
        // Legacy Python spellings of a finished run.
        "timed_out",
        "killed",
        "succeeded",
        "skipped",
        "terminated_after_answer",
    ];
    let owning = [
        "queued",
        "running",
        "starting",
        "claimed",
        "cancelling",
        "unknown",
        "limited",
        "needs_input",
        "orphaned",
        "stopped",
    ];
    for (index, name) in released.iter().chain(&owning).enumerate() {
        write_run(state.path(), &format!("run-{index:02}"), &status(name));
    }
    let scan = scan_runs(state.path());
    assert!(scan.unreadable.is_empty(), "{:?}", scan.unreadable);
    let held: Vec<_> = scan
        .records
        .iter()
        .map(|(_, r)| r["status"].as_str().unwrap())
        .collect();
    assert_eq!(held, owning);
}

#[test]
fn a_stopped_writer_holds_its_own_keys_and_blocks_no_other_key() {
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-stopped",
        &json!({"status":"stopped","role":"implement","item_key":"demo:OWN"}),
    );
    let own = [String::from("demo:OWN")];
    let error = error_of(admit_launch(&request(state.path(), &own)));
    assert!(error
        .message
        .contains("demo:OWN is held by live lane run-stopped"));
    assert_eq!(
        admit_launch(&request(state.path(), &[])).unwrap(),
        ["demo:ALPHA"]
    );
}

#[test]
fn a_finished_run_with_surviving_processes_keeps_its_keys() {
    let state = tempfile::tempdir().unwrap();
    for (id, survivors) in [("run-a", json!([{"pid":1}])), ("run-b", json!([]))] {
        write_run(
            state.path(),
            id,
            &json!({"status":"failed","role":"implement","item_key":format!("demo:{}", id),
                    "survivors":survivors}),
        );
    }
    let held: Vec<_> = scan_runs(state.path())
        .records
        .into_iter()
        .map(|(n, _)| n)
        .collect();
    assert_eq!(held, ["run-a"]);
    // A survivor list that is not a list cannot be judged.
    write_run(
        state.path(),
        "run-c",
        &json!({"status":"failed","role":"implement","survivors":"none"}),
    );
    assert_eq!(scan_runs(state.path()).unreadable, ["run-c"]);
}

#[test]
fn run_record_with_missing_or_non_string_status_holds_every_key() {
    let state = tempfile::tempdir().unwrap();
    let cases = [
        json!({"role":"implement","item_key":"demo:OTHER"}),
        json!({"role":"implement","status":null}),
        json!({"role":"implement","status":3}),
        json!({"role":"implement","status":"mystery"}),
        json!({"role":"review","status":null}),
        json!({"role":"review","status":["running"]}),
        json!({"role":"review","status":"mystery"}),
        json!({"status":"running"}),
        json!({"status":"running","role":null}),
        json!({"status":"running","role":7}),
        json!({"status":"running","role":"mystery"}),
        json!(["implement", "running"]),
        json!("running"),
    ];
    for (index, record) in cases.iter().enumerate() {
        let state = state.path().join(format!("s{index}"));
        write_run(&state, "run-bad", record);
        let error = error_of(admit_launch(&request(&state, &[])));
        assert_eq!(error.code, "lane_unreadable", "{record}");
        assert!(
            error.message.contains("unreadable run record run-bad"),
            "{record}: {}",
            error.message
        );
    }
}

#[test]
fn unreadable_run_state_refuses_first_and_a_keyless_launch_needs_no_run_state() {
    let state = tempfile::tempdir().unwrap();
    let run = state.path().join("runs/run-broken");
    fs::create_dir_all(&run).unwrap();
    // A directory in place of the record, a file that is not JSON, a vanished record.
    fs::create_dir(run.join("record.json")).unwrap();
    write_run(state.path(), "run-garbage", &json!(null));
    fs::write(
        state.path().join("runs/run-garbage/record.json"),
        "{not json",
    )
    .unwrap();
    fs::create_dir_all(state.path().join("runs/run-gone")).unwrap();
    let error = error_of(admit_launch(&request(state.path(), &[])));
    assert_eq!(error.code, "lane_unreadable");
    assert!(error.message.contains("run-broken") && error.message.contains("run-garbage"));
    assert!(!error.message.contains("run-gone"));
    let keyless = LaunchRequest {
        prompt: "No keys here\n",
        extra: &[],
        state: state.path(),
    };
    assert_eq!(admit_launch(&keyless).unwrap(), Vec::<String>::new());
    // The runs directory itself unreadable (a file in its place) holds every key too.
    let blocked = tempfile::tempdir().unwrap();
    fs::write(blocked.path().join("runs"), "x").unwrap();
    let error = error_of(admit_launch(&request(blocked.path(), &[])));
    assert_eq!(error.code, "lane_unreadable");
    assert!(error.message.contains("runs directory"));
}

#[test]
fn records_of_another_host_and_ended_records_with_old_roles_are_not_lanes() {
    let state = tempfile::tempdir().unwrap();
    // A receipt of a run on another host, whatever its status or role says.
    write_run(
        state.path(),
        "run-receipt",
        &json!({"status":"running","role":"implement","item_key":"demo:ALPHA","overflow_remote":true}),
    );
    write_run(
        state.path(),
        "run-origin",
        &json!({"status":"unknown","origin_receipt":true}),
    );
    // A refusal the validator persisted with the role it rejected.
    write_run(
        state.path(),
        "run-refused",
        &json!({"status":"refused","role":"bogus","item_key":"demo:ALPHA"}),
    );
    for (id, role) in [
        ("run-fix", "fix"),
        ("run-judge", "judge"),
        ("run-orch", "orchestrate"),
    ] {
        write_run(
            state.path(),
            id,
            &json!({"status":"running","role":role,"item_key":"demo:ALPHA"}),
        );
    }
    let scan = scan_runs(state.path());
    assert!(scan.records.is_empty() && scan.unreadable.is_empty());
    assert_eq!(
        admit_launch(&request(state.path(), &[])).unwrap(),
        ["demo:ALPHA"]
    );
}

#[test]
fn a_launch_holds_at_most_the_keys_a_run_record_keeps() {
    let extra: Vec<String> = (0..=MAX_KEYS).map(|n| format!("demo:K{n}")).collect();
    let error = error_of(admit("", &extra, &Fixed(vec![])));
    assert_eq!(error.code, "lane_too_many_keys");
    assert!(admit("", &extra[..MAX_KEYS], &Fixed(vec![])).is_ok());
}

#[test]
fn a_key_with_a_second_colon_is_one_key_in_briefs_runs_and_cards() {
    assert_eq!(
        item_keys("### demo:ITEM:detail | t\nWhy\n\n## demo:KEY: title\n"),
        ["demo:ITEM:detail", "demo:KEY"]
    );
    assert_eq!(normalize("demo:demo:ITEM:detail"), "demo:ITEM:detail");
    let runs = [(
        "run-1".to_owned(),
        json!({"run_id":"run-1","role":"implement","item_key":"demo:ITEM:detail"}),
    )];
    let lanes = run_lanes(&runs);
    assert_eq!(lanes[0].keys, ["demo:ITEM:detail"]);
    // After registration the full key is held and its prefix is not.
    let held = Fixed(lanes);
    assert!(admit("", &["demo:ITEM:detail".to_owned()], &held).is_err());
    assert!(admit("", &["demo:ITEM".to_owned()], &held).is_ok());
}

#[test]
fn record_keys_are_read_by_the_key_grammar_and_headings_by_their_separator() {
    // A run holds `demo:ITEM:` as stored: the heading parser must not trim it.
    let runs = [(
        "run-1".to_owned(),
        json!({"run_id":"run-1","role":"implement","item_key":"demo:ITEM:",
               "item_keys":["demo:demo:OTHER","bad key","demo:"]}),
    )];
    let lanes = run_lanes(&runs);
    assert_eq!(lanes[0].keys, ["demo:ITEM:", "demo:OTHER"]);
    let held = Fixed(lanes);
    assert!(admit("", &["demo:ITEM:".to_owned()], &held).is_err());
    assert!(admit("", &["demo:ITEM".to_owned()], &held).is_ok());
    let long = format!("demo:{}", "a".repeat(180));
    assert!(valid_key(&long));
    assert_eq!(
        item_keys(&format!("### {long}: title\n")),
        std::slice::from_ref(&long)
    );
    assert_eq!(item_keys(&format!("## {long} | title\n")), [long]);
    let too_long = format!("demo:{}", "a".repeat(181));
    assert!(item_keys(&format!("### {too_long}: title\n")).is_empty());
}

#[test]
fn a_brief_only_dispatch_meets_a_run_holding_a_key_with_a_trailing_colon() {
    let state = tempfile::tempdir().unwrap();
    write_run(
        state.path(),
        "run-held",
        &json!({"status":"running","role":"implement","item_key":"demo:ITEM:"}),
    );
    for brief in [
        "### demo:ITEM: | title\nWhy: x\n",
        "Item key: demo:ITEM:\n",
        "Plan item: demo:ITEM:\r\n",
        "## demo:ITEM:\n",
    ] {
        let dispatch = LaunchRequest {
            prompt: brief,
            extra: &[],
            state: state.path(),
        };
        let error = error_of(admit_launch(&dispatch));
        assert!(
            error
                .message
                .contains("demo:ITEM: is held by live lane run-held"),
            "{brief:?}"
        );
    }
    // The colon that separates a heading from its title text is not part of the key.
    assert_eq!(
        item_keys("## demo:ITEM: some title\n### demo:OTHER: more | x\nItem key: demo:THIRD: x\n"),
        ["demo:ITEM", "demo:OTHER", "demo:THIRD:"]
    );
    let dispatch = LaunchRequest {
        prompt: "## demo:ITEM: some title\n",
        extra: &[],
        state: state.path(),
    };
    assert!(admit_launch(&dispatch).is_ok());
}

fn typed_prompt(items: &[(&str, &str)]) -> String {
    let mut input: Value = serde_json::from_str(
        include_str!("../tests/fixtures/batch-writer-input.txt")
            .replace("\r\n", "\n")
            .strip_prefix("PLANGRAPH-BATCH-INPUT-V1\n")
            .unwrap(),
    )
    .unwrap();
    input["items"] = items
        .iter()
        .map(|(key, brief)| json!({"key":key,"title":"t","brief":brief}))
        .collect();
    format!("PLANGRAPH-BATCH-INPUT-V1\n{input}")
}

#[test]
fn a_versioned_batch_assigns_its_typed_keys_and_no_heading_of_an_item_body() {
    let prompt = typed_prompt(&[
        (
            "demo:ONE",
            "Why: x\n### demo:BODY | looks like an assignment\n",
        ),
        ("demo:demo:TWO", "Item key: demo:BODY2\n"),
    ]);
    assert_eq!(
        launch_keys(&prompt, &["demo:CARD".into()]).unwrap(),
        ["demo:ONE", "demo:TWO", "demo:CARD"]
    );
    assert!(launch_keys("PLANGRAPH-BATCH-INPUT-V1\n{", &[])
        .unwrap_err()
        .starts_with("invalid batch"));
    // A plain brief still assigns through its statement lines.
    assert_eq!(
        launch_keys("### demo:PLAIN | t\n", &[]).unwrap(),
        ["demo:PLAIN"]
    );
}

#[test]
fn admitted_item_keys_are_the_whole_membership_of_a_record_that_says_so() {
    let brief = "### demo:ONE | t\nWhy\n### demo:BODY | heading inside an item body\n";
    let runs = |marked: bool| {
        let mut record = json!({"role":"implement","item_keys":["demo:ONE"],
                                "manifest":{"brief":brief}});
        if marked {
            record["lane_keys_admitted"] = json!(true);
        }
        [("run-1".to_owned(), record)]
    };
    assert_eq!(run_lanes(&runs(true))[0].keys, ["demo:ONE"]);
    // A legacy record without the mark still holds the keys of its brief.
    assert_eq!(run_lanes(&runs(false))[0].keys, ["demo:BODY", "demo:ONE"]);
}
