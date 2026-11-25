use super::*;
use serde_json::json;
use std::fs;

struct Fixture {
    dir: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        Self {
            dir: tempfile::tempdir().unwrap(),
        }
    }
    fn state(&self) -> PathBuf {
        self.dir.path().join("state")
    }
    fn sessions(&self) -> PathBuf {
        self.dir.path().join("sessions")
    }
    fn native(&self, id: &str, status: &str, prompt: &str) {
        let run = self.state().join("runs").join(id);
        fs::create_dir_all(&run).unwrap();
        let record = json!({"run_id":id,"run_kind":"native-codex","status":status});
        fs::write(run.join("record.json"), record.to_string()).unwrap();
        let request = json!({"prompt":prompt,"card":{}});
        fs::write(run.join("native-request.json"), request.to_string()).unwrap();
    }
    fn claude(&self, session: &str, prompt: &str) {
        fs::create_dir_all(self.sessions()).unwrap();
        let brief = self.dir.path().join(format!("{session}.brief"));
        fs::write(&brief, prompt).unwrap();
        let record = json!({"name":"writer","session_name":session,"prompt_file":brief});
        fs::write(
            self.sessions().join(format!("{session}.json")),
            record.to_string(),
        )
        .unwrap();
    }
    fn check_with(
        &self,
        keys: &[&str],
        reason: Option<&str>,
        reserve: Option<(&str, &str)>,
    ) -> Result<(), String> {
        let keys = keys.iter().map(|k| (*k).to_owned()).collect();
        check(&Request {
            state: &self.state(),
            sessions: &self.sessions(),
            branches: &[],
            context: "test",
            keys: &keys,
            override_reason: reason,
            own: None,
            live: |_| None,
            record: true,
            reserve,
        })
    }
    fn check(&self, keys: &[&str], reason: Option<&str>) -> Result<(), String> {
        self.check_with(keys, reason, None)
    }
}

#[test]
fn a_native_lane_blocks_a_second_submission_and_is_named() {
    let fx = Fixture::new();
    fx.native(
        "run-a",
        "running",
        "Item key: plangraph:X-1\nNAME: batch1\n",
    );
    let error = fx.check(&["plangraph:X-1"], None).unwrap_err();
    assert!(
        error.contains("plangraph:X-1 is held by native run batch1 (run-a)"),
        "{error}"
    );
    assert_eq!(fx.check(&["plangraph:X-2"], None), Ok(()));
}

#[test]
fn a_claude_writer_lane_blocks_a_second_submission_and_is_named() {
    let fx = Fixture::new();
    fx.claude("writer-0007", "## plangraph:Y-1 | t\nNAME: batch2\n");
    let error = fx.check(&["plangraph:Y-1"], None).unwrap_err();
    assert!(
        error.contains("held by Claude writer batch2 (writer-0007)"),
        "{error}"
    );
}

#[test]
fn an_exited_claude_session_stops_holding_its_items() {
    let fx = Fixture::new();
    fx.claude("writer-0007", "Item key: plangraph:Y-1\nNAME: batch2\n");
    let record = fx.sessions().join("writer-0007.json");
    let mut row: Value = serde_json::from_str(&fs::read_to_string(&record).unwrap()).unwrap();
    row["session_id"] = json!("sid-1");
    fs::write(&record, row.to_string()).unwrap();
    let keys = ["plangraph:Y-1".to_owned()].into();
    let ask = |live: fn(Option<&str>) -> Option<BTreeSet<String>>| {
        check(&Request {
            state: &fx.state(),
            sessions: &fx.sessions(),
            branches: &[],
            context: "test",
            keys: &keys,
            override_reason: None,
            own: None,
            live,
            record: true,
            reserve: None,
        })
    };
    assert!(ask(|_| Some(["sid-1".to_owned()].into())).is_err());
    assert!(ask(|_| Some(BTreeSet::new())).is_ok(), "the session exited");
}

#[test]
fn a_finished_lane_holds_nothing() {
    let fx = Fixture::new();
    fx.native("run-a", "completed", "Item key: plangraph:X-1\n");
    assert_eq!(fx.check(&["plangraph:X-1"], None), Ok(()));
}

#[test]
fn an_override_needs_a_reason_and_is_recorded() {
    let fx = Fixture::new();
    fx.native(
        "run-a",
        "running",
        "Item key: plangraph:X-1\nNAME: batch1\n",
    );
    let blank = fx.check(&["plangraph:X-1"], Some(" ")).unwrap_err();
    assert!(blank.contains("requires a non-empty reason"), "{blank}");
    assert!(!fx.state().join(OVERRIDE_LOG).exists());
    assert_eq!(
        fx.check(&["plangraph:X-1"], Some("lane run-a is wedged")),
        Ok(())
    );
    let log = fs::read_to_string(fx.state().join(OVERRIDE_LOG)).unwrap();
    let line: Value = serde_json::from_str(log.trim()).unwrap();
    assert_eq!(line["reason"], "lane run-a is wedged");
    assert_eq!(line["held"][0]["key"], "plangraph:X-1");
    assert_eq!(line["context"], "test");
}

#[test]
fn two_submits_cannot_both_pass_the_check_before_either_publishes() {
    let fx = Fixture::new();
    let first = fx.check_with(&["plangraph:R-1"], None, Some(("run-one", "batch-one")));
    assert_eq!(first, Ok(()));
    let second = fx
        .check_with(&["plangraph:R-1"], None, Some(("run-two", "batch-two")))
        .unwrap_err();
    assert!(
        second.contains("held by native run batch-one (run-one)"),
        "{second}"
    );
    // The run that holds the reservation is never its own conflict.
    let again = fx.check_with(&["plangraph:R-1"], None, Some(("run-one", "batch-one")));
    assert_eq!(again, Ok(()));
    // Once the run publishes its record the record is the lane and the reservation lapses.
    fx.native("run-one", "completed", "Item key: plangraph:R-1\n");
    assert_eq!(fx.check(&["plangraph:R-1"], None), Ok(()));
}

#[test]
fn the_dispatch_check_reads_json_and_reports_a_refusal() {
    let fx = Fixture::new();
    fx.native(
        "run-a",
        "running",
        "Item key: plangraph:X-1\nNAME: batch1\n",
    );
    let input = |extra: Value| {
        let mut base = json!({"state":fx.state(),"sessions":fx.sessions(),
            "card":{"item_key":"plangraph:X-1"},"prompt":"","context":"dispatch"});
        base.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        base.to_string()
    };
    let refused = cli(&input(json!({}))).unwrap_err();
    assert!(
        refused.contains("plangraph:X-1 is held by native run batch1"),
        "{refused}"
    );
    assert!(cli(&input(json!({"reason":"wedged"}))).is_ok());
    assert!(cli(&input(json!({"reason":""}))).is_err());
    let preview = input(json!({"reason":"wedged","record":false}));
    let before = fs::read_to_string(fx.state().join(OVERRIDE_LOG)).unwrap();
    assert!(cli(&preview).is_ok());
    assert_eq!(
        fs::read_to_string(fx.state().join(OVERRIDE_LOG)).unwrap(),
        before
    );
}

#[test]
fn submission_keys_merge_the_brief_and_the_card() {
    let card = json!({"item_key":"plangraph:C-1","item_keys":["plangraph:C-2","bad key"]});
    let keys = submission_keys("Item key: plangraph:C-3\n", &card);
    let keys: Vec<_> = keys.iter().map(String::as_str).collect();
    assert_eq!(keys, ["plangraph:C-1", "plangraph:C-2", "plangraph:C-3"]);
}

#[test]
fn a_batch_input_carries_its_override_reason() {
    let text = format!(
        "PLANGRAPH-BATCH-INPUT-V1\n{}",
        include_str!("../tests/fixtures/batch-writer-input.txt")
            .strip_prefix("PLANGRAPH-BATCH-INPUT-V1\n")
            .unwrap()
            .replacen('{', "{\"lane_override\":\"second lane on purpose\",", 1)
    );
    assert_eq!(
        crate::batch_writer_brief::lane_override(&text).as_deref(),
        Some("second lane on purpose")
    );
    assert_eq!(
        crate::batch_writer_brief::lane_override("plain brief"),
        None
    );
}

#[test]
fn a_lock_that_cannot_be_taken_refuses_the_reservation() {
    let fx = Fixture::new();
    fs::create_dir_all(fx.state().join(LOCK_FILE)).unwrap();
    let error = fx
        .check_with(&["plangraph:L-1"], None, Some(("run-l", "lane-l")))
        .unwrap_err();
    assert!(error.contains("cannot take the admission lock"), "{error}");
    assert!(
        !fx.state().join("lane-reservations").exists(),
        "nothing was reserved"
    );
}

#[test]
fn the_canonical_keys_cover_the_prompt_the_alias_and_the_listed_items() {
    let input = json!({"prompt":"## demo:PROMPT | t\n","card":{"item":"demo:ALIAS",
        "item_keys":["demo:LISTED"]},"keys":["demo:EXPLICIT","bad key"]});
    let keys: Vec<String> = serde_json::from_str(&keys_cli(&input.to_string()).unwrap()).unwrap();
    assert_eq!(
        keys,
        ["demo:ALIAS", "demo:EXPLICIT", "demo:LISTED", "demo:PROMPT"]
    );
}
