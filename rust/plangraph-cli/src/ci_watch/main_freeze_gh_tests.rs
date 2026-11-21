use super::github::Github;
use super::main_freeze::{Diagnosis, Effects, Kind};
use super::main_freeze_gh::*;
use super::Task;
use crate::land::main_state::{Answer, Blocking, Failure, State};
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// `CI_WATCH_DELIVER_CMD` is process-wide: the tests that set it take this lock.
static ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

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
    let _env = ENV.lock().unwrap();
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

#[test]
fn intake_sweep_and_acknowledgement_share_one_locked_update_so_none_is_lost() {
    use super::batch_failure::FailureRecord;
    let dir = tempfile::tempdir().unwrap();
    let rec = |n: u64| FailureRecord {
        id: format!("f{n}"),
        batch: "b".into(),
        pr: n,
        run_id: n,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        created_at: 0,
        delivered_to: None,
        acked: false,
        escalated: false,
        claim: None,
    };
    let path = dir.path().to_owned();
    let threads: Vec<_> = (0..12)
        .map(|n| {
            let p = path.clone();
            std::thread::spawn(move || {
                update_failures(&p, |records| {
                    records.push(rec(n));
                    Ok(())
                })
                .unwrap();
            })
        })
        .collect();
    threads.into_iter().for_each(|t| t.join().unwrap());
    assert_eq!(
        load_failures(&path).unwrap().len(),
        12,
        "no concurrent update overwrote another"
    );
    // An acknowledgement is one more locked update.
    update_failures(&path, |records| {
        assert!(super::batch_failure::acknowledge(records, "f3"));
        Ok(())
    })
    .unwrap();
    assert!(load_failures(&path)
        .unwrap()
        .iter()
        .any(|r| r.id == "f3" && r.acked));
}

#[test]
fn the_supplied_service_runs_recovery_and_keeper_delivery() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let unit = std::fs::read_to_string(root.join("deploy/systemd/pg-ci-watch.service")).unwrap();
    let exec = unit.lines().find(|l| l.starts_with("ExecStart=")).unwrap();
    assert!(
        exec.contains("--keeper") && exec.contains("--main-freeze"),
        "{exec}"
    );
    let doc = std::fs::read_to_string(root.join("docs/ci-watch.md")).unwrap();
    assert!(
        doc.contains("ci-watch --keeper --main-freeze"),
        "the documented command carries both flags"
    );
}

#[test]
fn two_blocking_runs_with_the_same_diagnosis_have_distinct_fix_task_keys() {
    let dir = tempfile::tempdir().unwrap();
    let mut gh = Recorded {
        verbs: vec![],
        latest: 1,
    };
    let fx = GhEffects {
        gh: &mut gh,
        repo: "o/r".into(),
        dir: dir.path(),
        define: |_: &mut Task| Ok(false),
        close: |_: &str| Ok(()),
    };
    let d = Diagnosis {
        tests: vec!["same::test".into()],
        incomplete: false,
    };
    let (a, b) = (fx.fix_task_key(5, 0, &d), fx.fix_task_key(6, 0, &d));
    assert_ne!(a, b);
    assert_ne!(
        a,
        fx.fix_task_key(5, 1, &d),
        "another episode of the same run is another task"
    );
    assert_eq!(
        a,
        fx.fix_task_key(5, 0, &d),
        "the key is stable across retries"
    );
}

#[test]
fn delivery_runs_outside_the_lock_so_a_command_that_acknowledges_at_once_works() {
    use super::batch_failure::{Deliver, FailureRecord, Owners};
    struct AckingDeliver<'a>(&'a std::path::Path);
    impl Deliver for AckingDeliver<'_> {
        fn send(&mut self, _: &str, record: &FailureRecord) -> Result<(), String> {
            // The writer acknowledges synchronously: the same locked update `--ack` uses.
            update_failures(self.0, |records| {
                super::batch_failure::acknowledge(records, &record.id);
                Ok(())
            })
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let owners = Owners {
        prs: [("7".to_owned(), "b".to_owned())].into(),
        writers: [("b".to_owned(), "w1".to_owned())].into(),
        successors: Default::default(),
    };
    let task = Task {
        key: "k".into(),
        head: "pr-7".into(),
        pr: Some(7),
        run_id: 1,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
    };
    update_failures(dir.path(), |records| {
        Ok(super::batch_failure::record_failure(
            records, &owners, &task, 0,
        ))
    })
    .unwrap();
    deliver_pending(dir.path(), &owners, &mut AckingDeliver(dir.path()), &|| 0).unwrap();
    let records = load_failures(dir.path()).unwrap();
    assert!(
        records[0].acked,
        "the acknowledgement made during delivery is preserved"
    );
    assert_eq!(records[0].delivered_to.as_deref(), Some("w1"));
}

#[test]
fn two_passes_never_send_the_same_record_twice() {
    use super::batch_failure::{Deliver, FailureRecord, Owners};
    use std::sync::{Arc, Mutex};
    #[derive(Clone)]
    struct Counting(Arc<Mutex<Vec<String>>>);
    impl Deliver for Counting {
        fn send(&mut self, _: &str, record: &FailureRecord) -> Result<(), String> {
            self.0.lock().unwrap().push(record.id.clone());
            std::thread::sleep(std::time::Duration::from_millis(150));
            Ok(())
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let owners = Owners {
        prs: [("7".to_owned(), "b".to_owned())].into(),
        writers: [("b".to_owned(), "w1".to_owned())].into(),
        successors: Default::default(),
    };
    let task = Task {
        key: "k".into(),
        head: "pr-7".into(),
        pr: Some(7),
        run_id: 1,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
    };
    update_failures(dir.path(), |records| {
        Ok(super::batch_failure::record_failure(
            records, &owners, &task, 0,
        ))
    })
    .unwrap();
    let sent = Arc::new(Mutex::new(Vec::new()));
    let path = dir.path().to_owned();
    let owners = Arc::new(owners);
    let passes: Vec<_> = (0..2)
        .map(|_| {
            let (p, o, s) = (path.clone(), owners.clone(), sent.clone());
            std::thread::spawn(move || deliver_pending(&p, &o, &mut Counting(s), &|| 10).unwrap())
        })
        .collect();
    for t in passes {
        t.join().unwrap();
    }
    assert_eq!(
        sent.lock().unwrap().len(),
        1,
        "the claim keeps the second pass from sending it again"
    );
    let r = &load_failures(dir.path()).unwrap()[0];
    assert!(r.claim.is_none() && r.delivered_to.as_deref() == Some("w1"));
}

#[test]
fn a_failed_delivery_releases_its_claim_and_a_stale_claim_is_retried() {
    use super::batch_failure::{claim, pending_deliveries, Owners, CLAIM_SECS};
    let owners = Owners {
        prs: [("7".to_owned(), "b".to_owned())].into(),
        writers: [("b".to_owned(), "w1".to_owned())].into(),
        successors: Default::default(),
    };
    let task = Task {
        key: "k".into(),
        head: "pr-7".into(),
        pr: Some(7),
        run_id: 1,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
    };
    let mut records = vec![];
    let id = super::batch_failure::record_failure(&mut records, &owners, &task, 0).unwrap();
    let _ = claim(&mut records, &id, "w1", 100);
    assert!(pending_deliveries(&records, &owners, 100 + CLAIM_SECS - 1).is_empty());
    assert_eq!(
        pending_deliveries(&records, &owners, 100 + CLAIM_SECS).len(),
        1,
        "the claim went stale"
    );
    let token = records[0].claim.as_ref().unwrap().token.clone();
    super::batch_failure::settle(&mut records, &id, "w1", &token, false);
    assert_eq!(
        pending_deliveries(&records, &owners, 101).len(),
        1,
        "a failed delivery is released"
    );
}

#[test]
fn an_expired_delivery_cannot_clear_its_replacements_claim() {
    use super::batch_failure::{claim, pending_deliveries, settle, Owners, CLAIM_SECS};
    let owners = Owners {
        prs: [("7".to_owned(), "b".to_owned())].into(),
        writers: [("b".to_owned(), "w1".to_owned())].into(),
        successors: Default::default(),
    };
    let task = Task {
        key: "k".into(),
        head: "pr-7".into(),
        pr: Some(7),
        run_id: 1,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
    };
    let mut records = vec![];
    let id = super::batch_failure::record_failure(&mut records, &owners, &task, 0).unwrap();
    let first = claim(&mut records, &id, "w1", 100).unwrap();
    // The first delivery overruns; its claim goes stale and a replacement takes the record.
    let later = 100 + CLAIM_SECS;
    assert_eq!(pending_deliveries(&records, &owners, later).len(), 1);
    let second = claim(&mut records, &id, "w1", later).unwrap();
    assert_ne!(first, second);
    // The overrun delivery finishes (unsuccessfully): it must not release the replacement's claim.
    settle(&mut records, &id, "w1", &first, false);
    assert!(records[0].claim.is_some());
    assert!(
        pending_deliveries(&records, &owners, later + 1).is_empty(),
        "no third send while the replacement holds it"
    );
    // The replacement settles its own claim.
    settle(&mut records, &id, "w1", &second, true);
    assert!(records[0].claim.is_none() && records[0].delivered_to.as_deref() == Some("w1"));
}

#[test]
fn a_delivery_command_is_killed_at_its_lease() {
    use super::batch_failure::{Deliver, FailureRecord};
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("slow.sh");
    std::fs::write(&script, "#!/bin/sh\nsleep 30\n").unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let _env = ENV.lock().unwrap();
    std::env::set_var("CI_WATCH_DELIVER_CMD", &script);
    let record = FailureRecord {
        id: "f1".into(),
        batch: "b".into(),
        pr: 1,
        run_id: 1,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        created_at: 0,
        delivered_to: None,
        acked: false,
        escalated: false,
        claim: None,
    };
    let started = std::time::Instant::now();
    let r = CommandDeliver {
        dir: dir.path(),
        lease_secs: 1,
    }
    .send("w1", &record);
    std::env::remove_var("CI_WATCH_DELIVER_CMD");
    assert!(r.unwrap_err().contains("lease"));
    assert!(
        started.elapsed() < std::time::Duration::from_secs(10),
        "it did not wait for the 30 second command"
    );
}

#[test]
fn each_delivery_is_claimed_immediately_before_it_is_sent_not_when_the_pass_started() {
    use super::batch_failure::{Deliver, FailureRecord, Owners};
    struct Inspect<'a>(&'a std::path::Path, Vec<(String, usize)>);
    impl Deliver for Inspect<'_> {
        fn send(&mut self, _: &str, record: &FailureRecord) -> Result<(), String> {
            // At the moment of sending, only this record is claimed.
            let claimed = load_failures(self.0)
                .unwrap()
                .iter()
                .filter(|r| r.claim.is_some())
                .count();
            self.1.push((record.id.clone(), claimed));
            Ok(())
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let owners = Owners {
        prs: [
            ("7".to_owned(), "b".to_owned()),
            ("8".to_owned(), "b".to_owned()),
        ]
        .into(),
        writers: [("b".to_owned(), "w1".to_owned())].into(),
        successors: Default::default(),
    };
    for pr in [7, 8] {
        let task = Task {
            key: "k".into(),
            head: format!("pr-{pr}"),
            pr: Some(pr),
            run_id: pr,
            job: "j".into(),
            test: "t".into(),
            excerpt: "x".into(),
            priority: 10,
            keeper_defined: false,
            keeper_key: None,
        };
        update_failures(dir.path(), |records| {
            Ok(super::batch_failure::record_failure(
                records, &owners, &task, 0,
            ))
        })
        .unwrap();
    }
    let mut deliver = Inspect(dir.path(), vec![]);
    deliver_pending(dir.path(), &owners, &mut deliver, &|| 10).unwrap();
    assert_eq!(deliver.1.len(), 2);
    assert!(
        deliver.1.iter().all(|(_, claimed)| *claimed == 1),
        "one claim at a time: {:?}",
        deliver.1
    );
}

#[test]
fn an_expired_deliverys_success_does_not_overwrite_the_replacements_confirmed_delivery() {
    use super::batch_failure::{claim, settle, Owners};
    let owners = Owners {
        prs: [("7".to_owned(), "b".to_owned())].into(),
        writers: [("b".to_owned(), "w2".to_owned())].into(),
        successors: Default::default(),
    };
    let task = Task {
        key: "k".into(),
        head: "pr-7".into(),
        pr: Some(7),
        run_id: 1,
        job: "j".into(),
        test: "t".into(),
        excerpt: "x".into(),
        priority: 10,
        keeper_defined: false,
        keeper_key: None,
    };
    let mut records = vec![];
    let id = super::batch_failure::record_failure(&mut records, &owners, &task, 0).unwrap();
    let old = claim(&mut records, &id, "w1", 0).unwrap();
    // The batch was handed to w2; the stale claim is replaced and w2's delivery is confirmed.
    let new = claim(&mut records, &id, "w2", 400).unwrap();
    settle(&mut records, &id, "w2", &new, true);
    // The overrun delivery to the old writer finishes late and reports success: it changes nothing.
    settle(&mut records, &id, "w1", &old, true);
    assert_eq!(records[0].delivered_to.as_deref(), Some("w2"));
}

#[test]
fn every_claim_is_stamped_with_the_clock_read_for_that_claim() {
    use super::batch_failure::{Deliver, FailureRecord, Owners};
    use std::cell::Cell;
    struct Slow<'a>(&'a std::path::Path, &'a Cell<i64>, Vec<(String, i64, i64)>);
    impl Deliver for Slow<'_> {
        fn send(&mut self, _: &str, record: &FailureRecord) -> Result<(), String> {
            let claimed = load_failures(self.0)
                .unwrap()
                .iter()
                .find(|r| r.id == record.id)
                .and_then(|r| r.claim.clone())
                .unwrap();
            self.2.push((record.id.clone(), claimed.at, self.1.get()));
            self.1.set(self.1.get() + 155); // each command takes 155 seconds
            Ok(())
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let owners = Owners {
        prs: (1..=3).map(|n| (n.to_string(), "b".to_owned())).collect(),
        writers: [("b".to_owned(), "w1".to_owned())].into(),
        successors: Default::default(),
    };
    for pr in 1..=3u64 {
        let task = Task {
            key: "k".into(),
            head: format!("pr-{pr}"),
            pr: Some(pr),
            run_id: pr,
            job: "j".into(),
            test: "t".into(),
            excerpt: "x".into(),
            priority: 10,
            keeper_defined: false,
            keeper_key: None,
        };
        update_failures(dir.path(), |records| {
            Ok(super::batch_failure::record_failure(
                records, &owners, &task, 0,
            ))
        })
        .unwrap();
    }
    let clock = Cell::new(1000);
    let mut deliver = Slow(dir.path(), &clock, vec![]);
    deliver_pending(dir.path(), &owners, &mut deliver, &|| clock.get()).unwrap();
    assert_eq!(deliver.2.len(), 3);
    for (id, claimed_at, at_send) in &deliver.2 {
        assert_eq!(
            claimed_at, at_send,
            "{id} was claimed at the time it was sent, not when the pass began"
        );
    }
    assert_eq!(
        deliver.2[2].1, 1310,
        "the third claim is stamped after the two earlier commands"
    );
}
