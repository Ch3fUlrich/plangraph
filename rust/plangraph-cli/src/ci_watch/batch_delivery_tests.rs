use super::batch_failure::{Deliver, FailureRecord, Owners};
use super::main_freeze_gh::*;
use super::main_freeze_gh_tests::ENV;
use super::Task;

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

#[test]
fn the_deliverys_deadline_comes_from_its_claim_and_a_launch_after_it_is_skipped() {
    use super::batch_failure::{Deliver, FailureRecord, Owners, CLAIM_MARGIN_SECS, CLAIM_SECS};
    use std::cell::{Cell, RefCell};
    struct Recording(RefCell<Vec<u64>>);
    impl Deliver for Recording {
        fn send(&mut self, _: &str, _: &FailureRecord) -> Result<(), String> {
            unreachable!("deliver_pending bounds every send")
        }
        fn send_within(
            &mut self,
            _: &str,
            _: &FailureRecord,
            remaining: u64,
        ) -> Result<(), String> {
            self.0.borrow_mut().push(remaining);
            Ok(())
        }
    }
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
    for (delay, expect) in [
        (61, Some((CLAIM_SECS - CLAIM_MARGIN_SECS - 61) as u64)),
        (CLAIM_SECS - CLAIM_MARGIN_SECS, None),
    ] {
        let dir = tempfile::tempdir().unwrap();
        update_failures(dir.path(), |records| {
            Ok(super::batch_failure::record_failure(
                records, &owners, &task, 0,
            ))
        })
        .unwrap();
        // Clock reads: candidates (0), the claim (0), then the read before launch, `delay` seconds later.
        let calls = Cell::new(0);
        let clock = || {
            calls.set(calls.get() + 1);
            if calls.get() <= 2 {
                0
            } else {
                delay
            }
        };
        let mut deliver = Recording(RefCell::new(vec![]));
        deliver_pending(dir.path(), &owners, &mut deliver, &clock).unwrap();
        let seen = deliver.0.borrow().first().copied();
        assert_eq!(seen, expect, "delay {delay}");
        let record = &load_failures(dir.path()).unwrap()[0];
        assert!(record.claim.is_none(), "the claim is settled or released");
        assert_eq!(
            record.delivered_to.is_some(),
            expect.is_some(),
            "a skipped launch leaves it for the next pass"
        );
    }
}

#[test]
fn preparation_time_counts_against_the_deliverys_deadline_and_a_spent_budget_never_launches() {
    use super::batch_failure::{Deliver, FailureRecord};
    let dir = tempfile::tempdir().unwrap();
    let marker = dir.path().join("ran");
    let script = dir.path().join("mark.sh");
    std::fs::write(&script, format!("#!/bin/sh\ntouch {}\n", marker.display())).unwrap();
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
    let deliver = CommandDeliver {
        dir: dir.path(),
        lease_secs: 240,
    };
    // The budget started 2 seconds ago and is 1 second: the preparation already used it, nothing is launched.
    let long_ago = std::time::Instant::now() - std::time::Duration::from_secs(2);
    let spent = deliver.run_bounded("w1", &record, long_ago, std::time::Duration::from_secs(1));
    assert!(spent.unwrap_err().contains("budget spent"));
    assert!(!marker.exists(), "the command was not launched");
    // With budget left it runs, and a zero budget through the public entry point never launches either.
    assert!(deliver
        .run_bounded(
            "w1",
            &record,
            std::time::Instant::now(),
            std::time::Duration::from_secs(5)
        )
        .is_ok());
    assert!(marker.exists());
    std::fs::remove_file(&marker).unwrap();
    let mut zero = CommandDeliver {
        dir: dir.path(),
        lease_secs: 240,
    };
    assert!(zero.send_within("w1", &record, 0).is_err());
    std::env::remove_var("CI_WATCH_DELIVER_CMD");
    assert!(!marker.exists());
}
