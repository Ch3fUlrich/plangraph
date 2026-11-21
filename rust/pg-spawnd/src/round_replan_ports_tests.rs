use super::*;
use std::cell::RefCell;

/// Answers every command like the real tools and records `<tool> <args>`.
#[derive(Default)]
struct Recorder {
    calls: RefCell<Vec<String>>,
    defined: RefCell<Vec<Value>>,
    known: RefCell<Vec<String>>,
    close_fails: RefCell<bool>,
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
            (Tool::Spawner, "route", _) => Ok(json!({"legs":[
                {"id":"best", "roles":["implement","orchestrate"]},
                {"id":"second", "roles":["orchestrate"]},
                {"id":"review-only", "roles":["review"]}]})
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
            (Tool::Bus, "send", "--msg-id") => Ok("sent".into()),
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
