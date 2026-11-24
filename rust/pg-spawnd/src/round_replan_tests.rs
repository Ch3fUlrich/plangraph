use super::*;
use std::cell::RefCell;

fn leg(name: &str, intelligence: f64, planning: bool, available: bool) -> Leg {
    Leg {
        name: name.into(),
        intelligence,
        planning,
        available,
    }
}
fn change(path: &str, lines: u64) -> FileChange {
    FileChange {
        path: path.into(),
        lines,
    }
}

#[derive(Default)]
struct Fixture {
    calls: RefCell<Vec<String>>,
    fail_define_at: RefCell<Option<String>>,
    fail_send: RefCell<bool>,
    keys: RefCell<Vec<String>>,
    sent_keys: RefCell<Vec<String>>,
}
impl Fixture {
    fn count(&self, prefix: &str) -> usize {
        self.calls
            .borrow()
            .iter()
            .filter(|c| c.starts_with(prefix))
            .count()
    }
}
impl Ports for Fixture {
    fn legs(&self) -> Result<Vec<Leg>, ToolError> {
        Ok(vec![
            leg("weak", 30.0, true, true),
            leg("strong-down", 90.0, true, false),
            leg("strong", 70.0, true, true),
            leg("not-planning", 99.0, false, true),
        ])
    }
    fn pr_files(&self, _pr: &str) -> Result<Vec<FileChange>, ToolError> {
        Ok(vec![
            change("a/one.rs", 900),
            change("a/two.rs", 800),
            change("b/three.rs", 100),
            change("Cargo.lock", 5000),
            change("x/types.generated.rs", 4000),
        ])
    }
    fn item_text(&self, _item: &str) -> Result<String, ToolError> {
        Ok("Why: w\nHow: h".into())
    }
    fn open_findings(&self, _item: &str, _pr: &str) -> Result<Vec<String>, ToolError> {
        Ok(vec![
            "HIGH a/one.rs:3 keep\nverbatim".into(),
            "MED b/three.rs: two".into(),
        ])
    }
    fn plan(&self, leg: &Leg, input: &PlanInput) -> Result<Vec<Vec<String>>, ToolError> {
        self.calls.borrow_mut().push(format!(
            "plan {} {} files={} findings={} target={} text={}",
            leg.name,
            input.item,
            input.files.len(),
            input.findings.len(),
            input.target_lines,
            input.item_text.len()
        ));
        Ok(input.proposal.to_vec())
    }
    fn define_slice(&self, slice: &Slice) -> Result<(), ToolError> {
        if self.fail_define_at.borrow().as_deref() == Some(slice.key.as_str()) {
            return Err(parameter("keeper_down: define refused"));
        }
        self.calls
            .borrow_mut()
            .push(format!("define {}", slice.key));
        Ok(())
    }
    fn close_pr_unmerged(&self, pr: &str) -> Result<(), ToolError> {
        self.calls.borrow_mut().push(format!("close {pr}"));
        Ok(())
    }
    fn send_routing(&self, key: &str, line: &str) -> Result<(), ToolError> {
        self.sent_keys.borrow_mut().push(key.to_string());
        if *self.fail_send.borrow() {
            return Err(parameter("routing_down: send refused"));
        }
        // A receiver drops a repeat of a key it has already taken.
        if self.keys.borrow().iter().any(|k| k == key) {
            return Ok(());
        }
        self.keys.borrow_mut().push(key.to_string());
        self.calls.borrow_mut().push(format!("route {line}"));
        Ok(())
    }
}

/// A refusal of the real round admission after three rounds without a new best count.
pub(crate) fn refused_run(state: &Path) -> Value {
    let args = history(state, &[5, 5, 5]);
    let error = crate::round_progress::refused("no_progress");
    crate::round_progress::record_refusal(state, "fixture", &args, error)
        .ok()
        .unwrap()
}
fn history(dir: &Path, counts: &[u64]) -> crate::placement::Args {
    for (index, count) in counts.iter().enumerate() {
        let id = format!("run-r{}", index + 1);
        let source = dir.join(format!("verdict-{}.json", index + 1));
        let findings: Vec<_> = (0..*count)
            .map(|_| json!({"severity":"HIGH", "confirmed":true, "clear_fix":true, "text":"t"}))
            .collect();
        std::fs::write(
            &source,
            json!({"writer_run_id":id, "item_key":"p:I", "pr":"https://forge/p/1",
            "verdict":"REQUEST_CHANGES", "findings":findings})
            .to_string(),
        )
        .unwrap();
        crate::runs::create(
            dir,
            &id,
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "round_pr":"https://forge/p/1",
            "writer_round":index+1, "round_verdict_file":source}),
            pg_worker_core::run::record::Status::Completed,
        )
        .ok()
        .unwrap();
    }
    json!({"card":{"item_key":"p:I", "pr":"https://forge/p/1",
        "round_verdict_file":dir.join("next.json")}})
    .as_object()
    .unwrap()
    .clone()
}

#[test]
fn strongest_available_planning_leg_wins_and_none_is_refused() {
    let legs = Fixture::default().legs().ok().unwrap();
    assert_eq!(strongest_leg(&legs).unwrap().name, "strong");
    assert!(strongest_leg(&[leg("a", 5.0, false, true), leg("b", 5.0, true, false)]).is_none());
    // A tie goes to the lower name, so a replay picks the same leg.
    let tie = [leg("zed", 50.0, true, true), leg("amy", 50.0, true, true)];
    assert_eq!(strongest_leg(&tie).unwrap().name, "amy");
}

#[test]
fn slices_target_the_line_budget_and_skip_lockfiles_and_generated_files() {
    let files = counted(&Fixture::default().pr_files("1").ok().unwrap());
    let paths: Vec<_> = files.iter().map(|f| f.path.as_str()).collect();
    assert_eq!(paths, ["a/one.rs", "a/two.rs", "b/three.rs"]);
    let groups = group_files(&files, SLICE_TARGET_LINES);
    assert_eq!(
        groups,
        vec![vec!["a/one.rs"], vec!["a/two.rs", "b/three.rs"]]
    );
    let one = group_files(&[change("big.rs", 4000)], SLICE_TARGET_LINES);
    assert_eq!(one, vec![vec!["big.rs"]]);
}

#[test]
fn refusal_enqueues_one_job_and_a_replayed_trigger_changes_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let first = refused_run(dir.path());
    assert_eq!(first["reason"], "no_progress");
    assert_eq!(first["replan"], "queued");
    let again = refused_run(dir.path());
    assert!(again.get("replan").is_none());
    let jobs: Vec<_> = std::fs::read_dir(job_dir(dir.path())).unwrap().collect();
    assert_eq!(jobs.len(), 1);
    assert!(
        !enqueue(dir.path(), "p:I", "https://forge/p/1", "no_progress")
            .ok()
            .unwrap()
    );
    assert!(
        !enqueue(dir.path(), "p:I", "https://forge/p/1", "round_accepted")
            .ok()
            .unwrap()
    );
}

#[test]
fn job_closes_the_pr_once_defines_slices_with_findings_and_reports_once() {
    let dir = tempfile::tempdir().unwrap();
    refused_run(dir.path());
    let ports = Fixture::default();
    let done = run_pending(dir.path(), &ports);
    assert_eq!(done.len(), 1);
    assert_eq!(done[0].1, "done");
    assert_eq!(
        ports.count("plan strong p:I files=3 findings=2 target=1500"),
        1
    );
    assert_eq!(ports.count("close https://forge/p/1"), 1);
    assert_eq!(ports.count("define p:I-S"), 2);
    assert_eq!(ports.count("route"), 1);
    let routed = ports
        .calls
        .borrow()
        .iter()
        .find(|c| c.starts_with("route"))
        .cloned()
        .unwrap();
    assert_eq!(
        routed,
        "route REPLAN p:I no_progress | PR https://forge/p/1 closed unmerged | slices p:I-S1 p:I-S2"
    );
    let job = load(
        &std::fs::read_dir(job_dir(dir.path()))
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .find(|p| p.extension().is_some_and(|x| x == "json"))
            .unwrap(),
    )
    .ok()
    .unwrap();
    for slice in job["plan"]["slices"].as_array().unwrap() {
        assert_eq!(slice["findings"][0], "HIGH a/one.rs:3 keep\nverbatim");
        assert_eq!(slice["findings"][1], "MED b/three.rs: two");
    }
    // A replay of the whole run changes nothing.
    let before = ports.calls.borrow().len();
    refused_run(dir.path());
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.calls.borrow().len(), before);
}

#[test]
fn a_failed_step_is_retried_without_repeating_the_finished_ones() {
    let dir = tempfile::tempdir().unwrap();
    refused_run(dir.path());
    let ports = Fixture::default();
    *ports.fail_define_at.borrow_mut() = Some("p:I-S2".into());
    let first = run_pending(dir.path(), &ports);
    assert!(first[0].1.contains("keeper_down"));
    assert_eq!(
        (
            ports.count("define"),
            ports.count("close"),
            ports.count("route")
        ),
        (1, 0, 0)
    );
    *ports.fail_define_at.borrow_mut() = None;
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.count("define p:I-S1"), 1);
    assert_eq!(ports.count("define p:I-S2"), 1);
    assert_eq!(
        (
            ports.count("close"),
            ports.count("route"),
            ports.count("plan")
        ),
        (1, 1, 1)
    );
}

#[test]
fn a_failed_routing_send_is_retried_and_lands_once() {
    let dir = tempfile::tempdir().unwrap();
    refused_run(dir.path());
    let ports = Fixture::default();
    *ports.fail_send.borrow_mut() = true;
    assert!(run_pending(dir.path(), &ports)[0]
        .1
        .contains("routing_down"));
    assert_eq!((ports.count("close"), ports.count("route")), (1, 0));
    *ports.fail_send.borrow_mut() = false;
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!((ports.count("close"), ports.count("route")), (1, 1));
}

#[test]
fn a_plan_that_drops_a_file_is_refused_and_nothing_is_closed() {
    struct Dropping(Fixture);
    impl Ports for Dropping {
        fn legs(&self) -> Result<Vec<Leg>, ToolError> {
            self.0.legs()
        }
        fn pr_files(&self, pr: &str) -> Result<Vec<FileChange>, ToolError> {
            self.0.pr_files(pr)
        }
        fn item_text(&self, i: &str) -> Result<String, ToolError> {
            self.0.item_text(i)
        }
        fn open_findings(&self, i: &str, p: &str) -> Result<Vec<String>, ToolError> {
            self.0.open_findings(i, p)
        }
        fn plan(&self, _l: &Leg, _i: &PlanInput) -> Result<Vec<Vec<String>>, ToolError> {
            Ok(vec![vec!["a/one.rs".into()]])
        }
        fn define_slice(&self, s: &Slice) -> Result<(), ToolError> {
            self.0.define_slice(s)
        }
        fn close_pr_unmerged(&self, p: &str) -> Result<(), ToolError> {
            self.0.close_pr_unmerged(p)
        }
        fn send_routing(&self, k: &str, l: &str) -> Result<(), ToolError> {
            self.0.send_routing(k, l)
        }
    }
    let dir = tempfile::tempdir().unwrap();
    refused_run(dir.path());
    let ports = Dropping(Fixture::default());
    assert!(run_pending(dir.path(), &ports)[0]
        .1
        .contains("replan_plan_invalid"));
    assert!(ports.0.calls.borrow().is_empty());
}

fn job_file(state: &Path) -> PathBuf {
    std::fs::read_dir(job_dir(state))
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .find(|p| p.extension().is_some_and(|x| x == "json"))
        .unwrap()
}

#[test]
fn a_crash_after_the_send_mark_retries_the_delivery_and_reports_done_only_after_it() {
    let dir = tempfile::tempdir().unwrap();
    refused_run(dir.path());
    let ports = Fixture::default();
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    // The process died after the receiver took the line but before `sent` was saved.
    let path = job_file(dir.path());
    let mut job = load(&path).ok().unwrap();
    job["report"] = json!("sending");
    save(&path, &job).ok().unwrap();
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    let keys = ports.sent_keys.borrow();
    assert_eq!(keys.len(), 2);
    assert_eq!(keys[0], keys[1], "a retry carries the same idempotency key");
    assert_eq!(keys[0].len(), 32);
    assert!(keys[0]
        .bytes()
        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
    assert_eq!(ports.count("route"), 1, "the receiver dropped the repeat");
    assert_eq!(ports.count("close"), 1);
    assert_eq!(load(&path).ok().unwrap()["report"], "sent");
}

#[test]
fn a_crash_between_the_send_mark_and_the_delivery_still_sends_the_line() {
    let dir = tempfile::tempdir().unwrap();
    refused_run(dir.path());
    let ports = Fixture::default();
    *ports.fail_send.borrow_mut() = true;
    assert!(run_pending(dir.path(), &ports)[0]
        .1
        .contains("routing_down"));
    // The job stays in `sending`: a failed or interrupted delivery is never reported done.
    assert_eq!(
        load(&job_file(dir.path())).ok().unwrap()["report"],
        "sending"
    );
    *ports.fail_send.borrow_mut() = false;
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!(ports.count("route"), 1);
    assert_eq!(load(&job_file(dir.path())).ok().unwrap()["report"], "sent");
}

#[test]
fn an_escalation_replan_plans_on_the_strongest_leg_and_leaves_the_pr_open() {
    let dir = tempfile::tempdir().unwrap();
    assert!(
        enqueue_escalation(dir.path(), "p:I", "https://forge/p/1", 1)
            .ok()
            .unwrap()
    );
    assert!(
        !enqueue_escalation(dir.path(), "p:I", "https://forge/p/1", 1)
            .ok()
            .unwrap()
    );
    assert!(
        enqueue_escalation(dir.path(), "p:I", "https://forge/p/1", 2)
            .ok()
            .unwrap()
    );
    let ports = Fixture::default();
    let done = run_pending(dir.path(), &ports);
    assert!(done.iter().all(|(_, outcome)| outcome == "done"));
    assert_eq!(done.len(), 2);
    assert_eq!(ports.count("plan strong p:I"), 2);
    assert_eq!(ports.count("close"), 0);
    assert_eq!(ports.count("define"), 0);
    let routed = ports
        .calls
        .borrow()
        .iter()
        .find(|c| c.starts_with("route"))
        .cloned()
        .unwrap();
    assert_eq!(
        routed,
        "route REPLAN p:I escalated | PR https://forge/p/1 kept open | slices p:I-S1 p:I-S2"
    );
    // The saved record names the planning leg, the open PR and the nothing-defined state.
    for path in std::fs::read_dir(job_dir(dir.path()))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
    {
        let job = load(&path).ok().unwrap();
        assert_eq!(job["plan"]["leg"], "strong");
        assert_eq!(job["reason"], ESCALATED);
        assert_eq!(
            (job["closed"].clone(), job["defined"].clone()),
            (json!(false), json!([]))
        );
    }
    // One idempotency key per level.
    let keys = ports.keys.borrow();
    assert_eq!(keys.len(), 2);
    assert_ne!(keys[0], keys[1]);
}
