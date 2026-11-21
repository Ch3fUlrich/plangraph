use super::github::Github;
use super::main_freeze::{Diagnosis, Effects, Kind};
use super::main_freeze_gh::*;
use super::Task;
use crate::land::main_state::{Answer, Blocking, Failure, State};
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// Recorded GitHub replies for run 5: attempt 1 failed in job 11 whose log names a known flake.
struct Recorded {
    verbs: Vec<(u64, String)>,
    latest: u64,
}

const LOG: &str = "running 1 test\ntest flaky::test ... FAILED\n";

impl Github for Recorded {
    fn pages(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>, String> {
        assert!(endpoint.contains("/jobs"));
        Ok(vec![
            json!({"id": 11, "name": "rust", "conclusion": "failure"}),
            json!({"id": 12, "name": "scrub", "conclusion": "success"}),
        ])
    }
    fn page(&mut self, _: &str, _: Option<&str>) -> Result<Vec<Value>, String> {
        Err("unused".into())
    }
    fn log(&mut self, job: u64) -> Result<String, String> {
        assert_eq!(job, 11, "only failed jobs are read");
        Ok(LOG.into())
    }
    fn rerun(&mut self, _: u64) -> Result<(), String> {
        Err("unused".into())
    }
    fn run_verb(&mut self, run: u64, verb: &str) -> Result<(), String> {
        self.verbs.push((run, verb.into()));
        Ok(())
    }
    fn value(&mut self, endpoint: &str) -> Result<Value, String> {
        if endpoint == "actions/runs/5" {
            return Ok(json!({"run_attempt": self.latest}));
        }
        Ok(json!({"status": "completed", "conclusion": "failure"}))
    }
}

fn flakes() -> BTreeMap<String, String> {
    BTreeMap::from([("flaky::test".to_owned(), "plangraph:FLAKE-X".to_owned())])
}

fn red(at: i64) -> Answer {
    let blocking = Blocking {
        run: 5,
        event: "push".into(),
        attempt: 1,
        stalled: false,
        failures: vec![Failure::Suite {
            suite: "rust".into(),
            tests: vec![],
        }],
    };
    Answer {
        tip: "tip".into(),
        state: State::Red {
            blocking: vec![blocking],
        },
        runs: vec![],
        bound_t: 1800,
        at,
    }
}

#[test]
fn the_effects_read_the_recorded_attempt_and_request_one_failed_job_rerun() {
    let dir = tempfile::tempdir().unwrap();
    let mut gh = Recorded {
        verbs: vec![],
        latest: 1,
    };
    let mut fx = GhEffects {
        gh: &mut gh,
        repo: "o/r".into(),
        dir: dir.path(),
        define: |_: &mut Task| Ok(false),
        close: |_: &str| Ok(()),
    };
    assert_eq!(fx.attempts(5).unwrap(), (1, true));
    let d: Diagnosis = fx.diagnose(5, 1);
    assert_eq!(d.tests, vec!["flaky::test".to_owned()]);
    assert!(!d.incomplete);
    fx.request(5, Kind::RerunFailed).unwrap();
    assert_eq!(gh.verbs, vec![(5, "rerun-failed".to_owned())]);
}

#[test]
fn a_pass_reruns_the_flake_once_records_the_budget_and_survives_a_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut gh = Recorded {
        verbs: vec![],
        latest: 1,
    };
    {
        let mut fx = GhEffects {
            gh: &mut gh,
            repo: "o/r".into(),
            dir: dir.path(),
            define: |_: &mut Task| Ok(false),
            close: |_: &str| Ok(()),
        };
        let out = pass(dir.path(), &red(100), &flakes(), &mut fx, 100).unwrap();
        assert!(out.iter().any(|l| l.starts_with("requested RerunFailed")));
    }
    // A restarted watcher reads the persisted pending request: no second request while it is unreconciled.
    let mut fx = GhEffects {
        gh: &mut gh,
        repo: "o/r".into(),
        dir: dir.path(),
        define: |_: &mut Task| Ok(false),
        close: |_: &str| Ok(()),
    };
    pass(dir.path(), &red(200), &flakes(), &mut fx, 200).unwrap();
    assert_eq!(gh.verbs.len(), 1);
}

#[test]
fn a_spent_budget_is_published_for_routing() {
    let dir = tempfile::tempdir().unwrap();
    let mut gh = Recorded {
        verbs: vec![],
        latest: 1,
    };
    let mut fx = GhEffects {
        gh: &mut gh,
        repo: "o/r".into(),
        dir: dir.path(),
        define: |_: &mut Task| Ok(true),
        close: |_: &str| Ok(()),
    };
    let mut a = red(100);
    if let State::Red { blocking } = &mut a.state {
        blocking[0].failures = vec![Failure::Workflow { attempt: 1 }];
    }
    pass(dir.path(), &a, &flakes(), &mut fx, 100).unwrap();
    gh.latest = 2;
    let mut fx = GhEffects {
        gh: &mut gh,
        repo: "o/r".into(),
        dir: dir.path(),
        define: |_: &mut Task| Ok(true),
        close: |_: &str| Ok(()),
    };
    let mut b = red(300);
    if let State::Red { blocking } = &mut b.state {
        blocking[0].attempt = 2;
        blocking[0].failures = vec![Failure::Workflow { attempt: 2 }];
    }
    pass(dir.path(), &b, &flakes(), &mut fx, 300).unwrap();
    let published: Value =
        serde_json::from_slice(&std::fs::read(dir.path().join("alerts.json")).unwrap()).unwrap();
    let alerts = published["alerts"].as_array().unwrap();
    assert!(alerts
        .iter()
        .any(|a| a["kind"] == "recovery_budget_spent" && a["due"] == "routing"));
}

#[test]
fn a_train_failure_reaches_its_recorded_writer_and_defines_no_fix_lane() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("owners.json"),
        json!({"prs": {"7": "batch35"}, "writers": {"batch35": "writer-1"}, "successors": {"writer-1": "writer-2"}}).to_string(),
    )
    .unwrap();
    let script = dir.path().join("deliver.sh");
    std::fs::write(&script, "#!/bin/sh\necho \"$1\" > \"$2.sent\"\n").unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    // The delivery command is process-wide; this is the only test that sets it.
    std::env::set_var("CI_WATCH_DELIVER_CMD", &script);
    let mut task = Task {
        key: "plangraph:CI-FIX-1".into(),
        head: "pr-7".into(),
        pr: Some(7),
        run_id: 9,
        job: "rust".into(),
        test: "t::fails".into(),
        excerpt: "log tail".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
    };
    let mut keeper_called = false;
    let handled = route_or_define(dir.path(), &mut task, 100, |_| {
        keeper_called = true;
        Ok(true)
    })
    .unwrap();
    std::env::remove_var("CI_WATCH_DELIVER_CMD");
    assert!(handled && !keeper_called, "no unassigned fix lane");
    let records = load_failures(dir.path()).unwrap();
    assert_eq!(
        records[0].delivered_to.as_deref(),
        Some("writer-2"),
        "the successor holds the batch"
    );
    let sent = std::fs::read_dir(dir.path())
        .unwrap()
        .filter_map(|e| e.ok())
        .find(|e| e.file_name().to_string_lossy().ends_with(".sent"))
        .unwrap();
    assert_eq!(
        std::fs::read_to_string(sent.path()).unwrap().trim(),
        "writer-2"
    );
    // A pull request of no batch still takes the unchanged keeper path.
    let mut other = task.clone();
    other.pr = Some(8);
    assert!(route_or_define(dir.path(), &mut other, 100, |_| Ok(true)).unwrap());
}
