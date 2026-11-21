use super::*;
use std::cell::RefCell;

/// Answers every command like the real tools and records `<tool> <args>`.
#[derive(Default)]
struct Recorder {
    calls: RefCell<Vec<String>>,
    defined: RefCell<Vec<Value>>,
    known: RefCell<Vec<String>>,
    close_fails: RefCell<bool>,
    bus_queues: RefCell<bool>,
}
impl Recorder {
    fn count(&self, prefix: &str) -> usize {
        self.calls
            .borrow()
            .iter()
            .filter(|c| c.starts_with(prefix))
            .count()
    }
}
impl Exec for Recorder {
    fn run(&self, tool: Tool, args: &[String]) -> Result<String, ToolError> {
        self.calls
            .borrow_mut()
            .push(format!("{tool:?} {}", args.join(" ")));
        let word = |n: usize| args.get(n).map(String::as_str).unwrap_or("");
        match (tool, word(0), word(1)) {
            // The route lists the weaker leg first: the scores decide the planner.
            (Tool::Spawner, "route", _) => Ok(json!({"legs":[
                {"id":"first-listed", "roles":["implement","orchestrate"]},
                {"id":"strongest", "roles":["orchestrate"]},
                {"id":"unscored", "roles":["orchestrate"]},
                {"id":"review-only", "roles":["review"]}],
                "leg_scores":{"first-listed":10.0, "strongest":90.0, "unscored":null,
                    "review-only":99.0}})
            .to_string()),
            (Tool::Gh, "pr", "view") => Ok(json!({"files":[
                {"path":"a/one.rs", "additions":700, "deletions":200},
                {"path":"a/two.rs", "additions":800, "deletions":0},
                {"path":"Cargo.lock", "additions":9000, "deletions":0}]})
            .to_string()),
            (Tool::Gh, "pr", "close") if *self.close_fails.borrow() => {
                Err(parameter("replan_gh_failed: gh down"))
            }
            (Tool::Gh, "pr", "close") => Ok(String::new()),
            (Tool::Keeper, "--json", "show") if args[2] == "p:I" => Ok(json!({"key":"p:I",
                "kind":"lane", "track":"T3", "owner":"alice", "description":"Why: w\nHow: h"})
            .to_string()),
            (Tool::Keeper, "--json", "show") if self.known.borrow().contains(&args[2]) => {
                Ok("{}".into())
            }
            (Tool::Keeper, "--json", "show") => Err(parameter("not found")),
            (Tool::Keeper, "define", "--file") => {
                let body: Value =
                    serde_json::from_slice(&std::fs::read(&args[2]).unwrap()).unwrap();
                let item = body["items"][0].clone();
                self.known.borrow_mut().push(format!(
                    "{}:{}",
                    item["project"].as_str().unwrap(),
                    item["id"].as_str().unwrap()
                ));
                self.defined.borrow_mut().push(item);
                Ok(String::new())
            }
            (Tool::Bus, "send", "--msg-id") if *self.bus_queues.borrow() => {
                Ok(format!("queued {} (will retry)", args[2]))
            }
            (Tool::Bus, "send", "--msg-id") => Ok(format!("sent {} seq=1", args[2])),
            _ => panic!("unexpected command {tool:?} {args:?}"),
        }
    }
}

#[test]
fn a_refusal_runs_to_completion_through_the_command_ports() {
    let dir = tempfile::tempdir().unwrap();
    let refused = replan::tests::refused_run(dir.path());
    assert_eq!(refused["replan"], "queued");
    let exec = Recorder::default();
    let done = pass(dir.path(), &exec);
    assert_eq!(done.len(), 1);
    assert_eq!(done[0].1, "done");
    // The PR's files and the legs come from gh and the route; the lockfile is not counted.
    assert_eq!(exec.count("Gh pr view https://forge/p/1 --json files"), 1);
    assert_eq!(exec.count("Spawner route --role orchestrate --json"), 1);
    // Two slices of 1500 target lines: a/one.rs (900) and a/two.rs (800) split.
    let defined = exec.defined.borrow();
    assert_eq!(defined.len(), 2);
    assert_eq!(defined[0]["id"], "I-S1");
    assert_eq!(defined[0]["project"], "p");
    assert_eq!(defined[0]["track"], "T3");
    assert_eq!(defined[0]["owner"], "alice");
    // Every slice hangs under its parent: the keeper rejects an item with no path to a goal.
    assert_eq!(defined[0]["under"], "p:I");
    assert_eq!(defined[1]["under"], "p:I");
    // The planner is the leg with the highest catalog score, not the first one the route lists.
    let job = std::fs::read_dir(dir.path().join("replans"))
        .unwrap()
        .filter_map(|e| e.ok())
        .find(|e| e.path().extension().is_some_and(|x| x == "json"))
        .unwrap();
    let job: Value = serde_json::from_slice(&std::fs::read(job.path()).unwrap()).unwrap();
    assert_eq!(job["plan"]["leg"], "strongest");
    // Every open finding of the last round rides on every slice, verbatim.
    for item in defined.iter() {
        assert!(item["description"].as_str().unwrap().contains("HIGH t"));
        assert!(item["description"].as_str().unwrap().contains("Why:"));
        assert!(item["description"].as_str().unwrap().contains("How:"));
    }
    assert_eq!(exec.count("Gh pr close https://forge/p/1"), 1);
    let sent: Vec<_> = exec
        .calls
        .borrow()
        .iter()
        .filter(|c| c.starts_with("Bus send --msg-id"))
        .cloned()
        .collect();
    assert_eq!(sent.len(), 1);
    assert!(sent[0].contains("routing REPLAN p:I no_progress | PR https://forge/p/1 closed unmerged | slices p:I-S1 p:I-S2"));
    // The next pass changes nothing.
    let before = exec.calls.borrow().len();
    assert_eq!(pass(dir.path(), &exec)[0].1, "done");
    assert_eq!(exec.calls.borrow().len(), before);
}

#[test]
fn a_failed_close_leaves_the_slices_defined_once_and_the_retry_finishes() {
    let dir = tempfile::tempdir().unwrap();
    replan::tests::refused_run(dir.path());
    let exec = Recorder::default();
    *exec.close_fails.borrow_mut() = true;
    assert!(pass(dir.path(), &exec)[0].1.contains("gh down"));
    assert_eq!(exec.count("Bus send"), 0);
    *exec.close_fails.borrow_mut() = false;
    assert_eq!(pass(dir.path(), &exec)[0].1, "done");
    assert_eq!(exec.defined.borrow().len(), 2);
    assert_eq!(exec.count("Bus send"), 1);
}

#[test]
fn a_slice_the_keeper_already_knows_is_not_defined_again() {
    let dir = tempfile::tempdir().unwrap();
    replan::tests::refused_run(dir.path());
    let exec = Recorder::default();
    exec.known.borrow_mut().push("p:I-S1".into());
    assert_eq!(pass(dir.path(), &exec)[0].1, "done");
    let defined = exec.defined.borrow();
    assert_eq!(defined.len(), 1);
    assert_eq!(defined[0]["id"], "I-S2");
}

#[test]
fn the_spawn_server_starts_the_replan_runner() {
    // Production wiring: both serve entry points start the runner before they accept a client.
    let source =
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/serve.rs")).unwrap();
    assert_eq!(
        source
            .matches("round_replan_ports::start(state.clone())")
            .count(),
        2
    );
}

#[test]
fn a_locally_queued_line_is_not_an_acknowledgment_and_the_retry_reuses_the_id() {
    let dir = tempfile::tempdir().unwrap();
    replan::tests::refused_run(dir.path());
    let exec = Recorder::default();
    *exec.bus_queues.borrow_mut() = true;
    assert!(pass(dir.path(), &exec)[0]
        .1
        .contains("replan_bus_unacknowledged"));
    *exec.bus_queues.borrow_mut() = false;
    assert_eq!(pass(dir.path(), &exec)[0].1, "done");
    let ids: Vec<String> = exec
        .calls
        .borrow()
        .iter()
        .filter(|c| c.starts_with("Bus send --msg-id "))
        .map(|c| c.split(' ').nth(3).unwrap().to_string())
        .collect();
    assert_eq!(ids.len(), 2);
    assert_eq!(ids[0], ids[1]);
    // Acknowledged now: no third send.
    assert_eq!(pass(dir.path(), &exec)[0].1, "done");
    assert_eq!(exec.count("Bus send"), 2);
}

/// A later round that never produced a verdict, or was voided, hides nothing.
#[test]
fn findings_come_from_the_latest_validated_round_of_the_pr() {
    let dir = tempfile::tempdir().unwrap();
    replan::tests::refused_run(dir.path()); // rounds 1-3, five HIGH each
    let add = |id: &str, round: u64, status, extra: Value| {
        let mut base = json!({"role":"implement", "item_key":"p:I", "round_pr":"https://forge/p/1",
            "writer_round":round, "round_verdict_file":dir.path().join("missing.json")});
        base.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        crate::runs::create(dir.path(), id, "fixture", base, status)
            .ok()
            .unwrap();
    };
    use pg_worker_core::run::record::Status;
    add("run-r4", 4, Status::Failed, json!({"round_void":true}));
    add("run-r5", 5, Status::Completed, json!({}));
    let found = last_round_findings(dir.path(), "p:I", "https://forge/p/1")
        .ok()
        .unwrap();
    assert_eq!(found, vec!["HIGH t"; 5]);
    assert!(last_round_findings(dir.path(), "p:I", "https://forge/p/2")
        .ok()
        .unwrap()
        .is_empty());
}

/// The generated slice definitions are the golden file `tests/fixtures/replan_slice_definition.json`;
/// `tests/test_replan_slice_definition.py` feeds the same file to the real keeper store.
#[test]
fn the_generated_slice_definitions_match_the_golden_file_the_keeper_test_uses() {
    let dir = tempfile::tempdir().unwrap();
    replan::tests::refused_run(dir.path());
    let exec = Recorder::default();
    assert_eq!(pass(dir.path(), &exec)[0].1, "done");
    let golden = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/replan_slice_definition.json");
    let items = json!({"items":exec.defined.borrow().clone()});
    if std::env::var_os("PG_WRITE_GOLDEN").is_some() {
        std::fs::write(
            &golden,
            serde_json::to_string_pretty(&items).unwrap() + "\n",
        )
        .unwrap();
    }
    let want: Value = serde_json::from_slice(&std::fs::read(&golden).unwrap()).unwrap();
    assert_eq!(items, want);
}
