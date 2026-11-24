use super::launch_tests::{request, run_holding};
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

fn check_brief(
    brief: &str,
    source: &dyn LaneSource,
    override_reason: Option<&str>,
) -> Result<Option<String>, String> {
    admit(brief, &[], source, override_reason).map(|(reason, _)| reason)
}

const BRIEF: &str = "### demo:ALPHA | title\nWhy: x\n\n### demo:BETA | title\nWhy: y\n";

#[test]
fn refuses_a_key_of_another_live_lane_and_names_the_lane() {
    let error = check_brief(BRIEF, &held("demo:BETA"), None).unwrap_err();
    assert!(error.starts_with("lane_collision: demo:BETA is held by live lane writer-0673"));
    assert!(!error.contains("demo:ALPHA"));
}

#[test]
fn a_free_key_passes() {
    assert_eq!(check_brief(BRIEF, &held("demo:OTHER"), None), Ok(None));
}

#[test]
fn override_with_a_reason_passes_and_is_returned() {
    let reason = check_brief(BRIEF, &held("demo:ALPHA"), Some("  fix lane  "));
    assert_eq!(reason, Ok(Some("fix lane".into())));
}

#[test]
fn blank_override_is_refused_even_without_a_collision() {
    for blank in ["", "  \t"] {
        let error = check_brief(BRIEF, &held("demo:OTHER"), Some(blank)).unwrap_err();
        assert_eq!(error, "lane_override requires a nonblank reason");
    }
}

#[test]
fn an_override_line_in_the_brief_text_is_not_an_override() {
    let brief = format!("{BRIEF}LANE_OVERRIDE: second lane on purpose\n");
    assert!(check_brief(&brief, &held("demo:ALPHA"), None).is_err());
}

#[test]
fn live_runs_hold_their_item_key_brief_keys_and_recorded_items() {
    let runs = [
        json!({"run_id":"run-1","role":"implement","item_key":"demo:ONE"}),
        json!({"run_id":"run-2","role":"implement",
               "manifest":{"brief":"### demo:TWO | t\nWhy\n"}}),
        json!({"run_id":"run-3","role":"implement","fleet_session_name":"writer-0009",
               "item_keys":["demo:THREE"]}),
        json!({"run_id":"run-4","role":"implement","item_key":"bad key"}),
        json!({"run_id":"run-5","role":"review","item_key":"demo:ONE"}),
    ];
    let lanes = run_lanes(&runs);
    let by: Vec<_> = lanes
        .iter()
        .map(|l| (l.name.as_str(), l.keys.clone()))
        .collect();
    assert_eq!(
        by,
        [
            ("run-1", vec!["demo:ONE".to_string()]),
            ("run-2", vec!["demo:TWO".to_string()]),
            ("writer-0009", vec!["demo:THREE".to_string()]),
        ]
    );
}

#[test]
fn only_queued_or_running_records_are_live() {
    let dir = tempfile::tempdir().unwrap();
    for (id, status) in [
        ("run-a", "running"),
        ("run-b", "completed"),
        ("run-c", "queued"),
    ] {
        let run = dir.path().join("runs").join(id);
        fs::create_dir_all(&run).unwrap();
        fs::write(
            run.join("record.json"),
            json!({"run_id":id,"status":status,"item_key":"demo:K"}).to_string(),
        )
        .unwrap();
    }
    let ids: Vec<_> = scan_runs(dir.path())
        .records
        .iter()
        .map(|r| r["run_id"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(ids, ["run-a", "run-c"]);
}

#[test]
fn a_typed_prompt_override_does_not_admit_a_held_key() {
    let dir = tempfile::tempdir().unwrap();
    let run = dir.path().join("runs/run-held");
    fs::create_dir_all(&run).unwrap();
    fs::write(
        run.join("record.json"),
        json!({"run_id":"run-held","status":"running","role":"implement",
               "item_key":"demo:FIXTURE"})
        .to_string(),
    )
    .unwrap();
    let mut input: Value = serde_json::from_str(
        include_str!("../tests/fixtures/batch-writer-input.txt")
            .replace("\r\n", "\n")
            .strip_prefix("PLANGRAPH-BATCH-INPUT-V1\n")
            .unwrap(),
    )
    .unwrap();
    input["lane_override"] = json!("typed reason");
    let prompt = format!("PLANGRAPH-BATCH-INPUT-V1\n{input}");
    let mut typed = request(dir.path(), &[]);
    typed.prompt = &prompt;
    let error = admit_launch(&typed).unwrap_err();
    assert!(
        error.contains("demo:FIXTURE is held by live lane run-held"),
        "{error}"
    );
    // The card field is the override a dispatch admits.
    typed.explicit = Some("card reason");
    let launched = admit_launch(&typed).unwrap();
    assert_eq!(launched.lane_override.as_deref(), Some("card reason"));
}

#[test]
fn a_key_with_a_second_colon_is_one_key_in_briefs_runs_and_cards() {
    assert_eq!(
        item_keys("### demo:ITEM:detail | t\nWhy\n\n## demo:KEY: title\n"),
        ["demo:ITEM:detail", "demo:KEY"]
    );
    assert_eq!(normalize("demo:demo:ITEM:detail"), "demo:ITEM:detail");
    let runs = [json!({"run_id":"run-1","role":"implement","item_key":"demo:ITEM:detail"})];
    let lanes = run_lanes(&runs);
    assert_eq!(lanes[0].keys, ["demo:ITEM:detail"]);
    // After registration the full key is held and its prefix is not.
    let held = Fixed(lanes);
    let full = ["demo:ITEM:detail".to_owned()];
    assert!(admit("", &full, &held, None).is_err());
    let short = ["demo:ITEM".to_owned()];
    assert!(admit("", &short, &held, None).is_ok());
}

#[test]
fn record_keys_are_read_by_the_key_grammar_and_headings_by_their_separator() {
    // A run holds `demo:ITEM:` as stored: the heading parser must not trim it.
    let runs = [
        json!({"run_id":"run-1","role":"implement","item_key":"demo:ITEM:",
                       "item_keys":["demo:demo:OTHER","bad key","demo:"]}),
    ];
    let lanes = run_lanes(&runs);
    assert_eq!(lanes[0].keys, ["demo:ITEM:", "demo:OTHER"]);
    let held = Fixed(lanes);
    assert!(admit("", &["demo:ITEM:".to_owned()], &held, None).is_err());
    assert!(admit("", &["demo:ITEM".to_owned()], &held, None).is_ok());
    // A longest valid id still ends at the colon of the heading's title.
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
    let dir = tempfile::tempdir().unwrap();
    run_holding(dir.path(), "demo:ITEM:");
    for brief in [
        "### demo:ITEM: | title\nWhy: x\n",
        "Item key: demo:ITEM:\n",
        "Plan item: demo:ITEM:\r\n",
        "## demo:ITEM:\n",
    ] {
        let mut dispatch = request(dir.path(), &[]);
        dispatch.prompt = brief;
        let error = admit_launch(&dispatch).unwrap_err();
        assert!(
            error.contains("demo:ITEM: is held by live lane run-held"),
            "{brief:?}: {error}"
        );
    }
    // The colon that separates a heading from its title text is not part of the key.
    assert_eq!(
        item_keys("## demo:ITEM: some title\n### demo:OTHER: more | x\nItem key: demo:THIRD: x\n"),
        ["demo:ITEM", "demo:OTHER", "demo:THIRD:"]
    );
    let mut dispatch = request(dir.path(), &[]);
    dispatch.prompt = "## demo:ITEM: some title\n";
    assert!(admit_launch(&dispatch).is_ok());
}
