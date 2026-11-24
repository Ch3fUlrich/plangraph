use super::{group::Confinement, record::RunRecord, supervise::*, supervisor::*};
use std::{
    path::Path,
    thread,
    time::{Duration, Instant},
};

fn config(dir: &Path, cap: usize, queue: usize) -> SupervisorCfg {
    let mut cfg = SupervisorCfg::new(Config {
        state_dir: dir.into(),
        confinement: Confinement::Plain,
        grace: Duration::from_millis(30),
        kill_timeout: Duration::from_millis(50),
        reader_grace: Duration::from_millis(50),
        held_poll: Duration::from_millis(10),
        ..Default::default()
    });
    cfg.max_runs = cap;
    cfg.max_queued = queue;
    cfg
}
fn spec(dir: &Path, n: usize, script: &str) -> RunSpec {
    RunSpec {
        run_id: format!("run-{n}"),
        job_id: "job-test".into(),
        program: "sh".into(),
        args: vec!["-c".into(), script.into()],
        cwd: dir.into(),
        env: vec![],
        stdin: None,
        base: Default::default(),
        events: vec![],
        finish: None,
        allow_shas: Default::default(),
        review_slot: None,
    }
}
fn until(mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(4);
    while !f() {
        assert!(Instant::now() < deadline, "deadline exceeded");
        thread::sleep(Duration::from_millis(10));
    }
}
fn read(s: &Supervisor, n: usize) -> serde_json::Value {
    RunRecord::read(&s.config().state_dir, &format!("run-{n}"))
        .unwrap()
        .to_value()
}
#[test]
fn cap_and_fifo() {
    let d = tempfile::tempdir().unwrap();
    let s = Supervisor::new(config(d.path(), 2, 64)).unwrap();
    for n in 0..5 {
        s.start(spec(d.path(), n, HOLD)).unwrap();
    }
    until(|| {
        let c = s.counts();
        c.running == 2 && c.queued == 3
    });
    std::fs::write(d.path().join("go"), "").unwrap();
    until(|| {
        let c = s.counts();
        assert!(c.running <= 2);
        c.running == 0 && c.queued == 0
    });
    for n in 0..5 {
        assert!(s.wait_finished(&format!("run-{n}"), Duration::from_secs(1)));
        assert_eq!(read(&s, n)["status"], "completed");
    }
    assert_eq!(s.max_held_seen(), 2);

    // With one permit, each append finishes before the next grant.
    let s = Supervisor::new(config(d.path(), 1, 64)).unwrap();
    for n in 5..10 {
        let script = format!("{HOLD}; echo {} >> order", n - 5);
        s.start(spec(d.path(), n, &script)).unwrap();
    }
    for n in 5..10 {
        assert!(s.wait_finished(&format!("run-{n}"), Duration::from_secs(1)));
        assert_eq!(read(&s, n)["status"], "completed");
    }
    assert_eq!(
        std::fs::read_to_string(d.path().join("order")).unwrap(),
        "0\n1\n2\n3\n4\n"
    );
}

#[test]
fn queued_records_cancel_head_and_promote() {
    for cancel_head in [false, true] {
        let d = tempfile::tempdir().unwrap();
        let s = Supervisor::new(config(d.path(), 1, 64)).unwrap();
        s.start(gated_spec(d.path())).unwrap();
        until(|| read(&s, 0)["status"] == "running");
        for n in 1..4 {
            s.start(spec(d.path(), n, "sleep .2")).unwrap();
        }
        assert_eq!(
            s.counts(),
            Counts {
                total_started: 4,
                running: 1,
                queued: 3
            }
        );
        assert!(s.is_owned("run-0"));
        assert!(!s.wait_finished("run-0", Duration::ZERO));
        assert_eq!(read(&s, 1)["status"], "queued");
        assert!(read(&s, 1).get("pid").is_none());
        let cancelled = if cancel_head { 1 } else { 2 };
        assert_eq!(
            s.cancel(&format!("run-{cancelled}")),
            CancelOutcome::Requested
        );
        assert!(s.wait_finished(&format!("run-{cancelled}"), Duration::from_secs(1)));
        assert_eq!(s.counts().queued, 2);
        let v = read(&s, cancelled);
        assert_eq!(v["status"], "cancelled");
        assert!(v.get("pid").is_none());
        assert!(!d
            .path()
            .join(format!("runs/run-{cancelled}/live.log"))
            .exists());
        std::fs::write(d.path().join("go"), "").unwrap();
        let next = if cancel_head { 2 } else { 1 };
        until(|| read(&s, next)["status"] == "running");
        assert!(read(&s, next)["pid"].is_number());
        for n in 0..4 {
            assert!(s.wait_finished(&format!("run-{n}"), Duration::from_secs(2)));
        }
        assert!(!s.is_owned("run-0"));
        assert_eq!(s.cancel("run-0"), CancelOutcome::Finished);
        assert_eq!(s.cancel("run-unknown"), CancelOutcome::Unknown);
        assert!(s.wait_finished("run-unknown", Duration::ZERO));
        assert_eq!(
            s.counts(),
            Counts {
                total_started: 4,
                running: 0,
                queued: 0
            }
        );
    }
}

fn gated_spec(dir: &Path) -> RunSpec {
    spec(
        dir,
        0,
        "i=0; while [ ! -e go ] && [ $i -lt 400 ]; do sleep .01; i=$((i+1)); done",
    )
}

#[test]
fn limits_refusal_and_failed_start_rollback() {
    let d = tempfile::tempdir().unwrap();
    for (cap, queued) in [(0, 64), (65, 64), (1, 1025)] {
        assert!(matches!(
            Supervisor::new(config(d.path(), cap, queued)),
            Err(SupervisorError::BadCap)
        ));
    }
    let defaults = SupervisorCfg::new(Config::default());
    assert_eq!((defaults.max_runs, defaults.max_queued), (3, 64));
    for queue in [0, 2] {
        let d = tempfile::tempdir().unwrap();
        let s = Supervisor::new(config(d.path(), 1, queue)).unwrap();
        s.start(gated_spec(d.path())).unwrap();
        until(|| s.counts().running == 1);
        for n in 1..=queue {
            s.start(spec(d.path(), n, "true")).unwrap();
        }
        let refused = queue + 1;
        assert!(matches!(
            s.start(spec(d.path(), refused, "true")),
            Err(SupervisorError::QueueFull)
        ));
        assert!(!d.path().join(format!("runs/run-{refused}")).exists());
        assert!(!s.is_owned(&format!("run-{refused}")));
        assert_eq!(s.counts().total_started, (queue + 1) as u64);
        std::fs::write(d.path().join("go"), "").unwrap();
        for n in 0..=queue {
            assert!(s.wait_finished(&format!("run-{n}"), Duration::from_secs(2)));
        }
    }
    let s = Supervisor::new(config(d.path(), 1, 2)).unwrap();
    let mut bad = spec(d.path(), 0, "true");
    bad.base.insert("pid".into(), serde_json::json!(0));
    assert!(matches!(
        s.start(bad),
        Err(SupervisorError::Start(StartError::Reserved(_)))
    ));
    assert_eq!(
        s.counts(),
        Counts {
            total_started: 0,
            running: 0,
            queued: 0
        }
    );
    s.start(spec(d.path(), 1, "true")).unwrap();
    assert!(s.wait_finished("run-1", Duration::from_secs(1)));
}

#[test]
fn survivors_keep_slot_and_ownership() {
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    let d = tempfile::tempdir().unwrap();
    let empty = Arc::new(AtomicBool::new(false));
    let s = Supervisor::with_launcher(
        config(d.path(), 1, 64),
        Arc::new(super::testutil::Survivors(empty.clone())),
    )
    .unwrap();
    s.start(spec(d.path(), 0, "true")).unwrap();
    until(|| read(&s, 0)["status"] == "failed");
    s.start(spec(d.path(), 1, "true")).unwrap();
    let deadline = Instant::now() + Duration::from_millis(300);
    while Instant::now() < deadline {
        assert_eq!(s.counts().running, 1);
        assert_eq!(s.counts().queued, 1);
        assert!(read(&s, 1).get("pid").is_none());
        thread::sleep(Duration::from_millis(10));
    }
    assert!(s.is_owned("run-0"));
    assert!(!s.wait_finished("run-0", Duration::from_millis(30)));
    assert!(read(&s, 1).get("pid").is_none());
    empty.store(true, Ordering::SeqCst);
    for n in 0..2 {
        assert!(s.wait_finished(&format!("run-{n}"), Duration::from_secs(1)));
    }
    assert_eq!(read(&s, 1)["status"], "completed");
}

#[test]
fn stress_and_drop_does_not_cancel() {
    let d = tempfile::tempdir().unwrap();
    let s = Supervisor::new(config(d.path(), 3, 64)).unwrap();
    // All queue waves share one deadline, including durable lifecycle writes.
    let budget = Duration::from_secs(30);
    let deadline = Instant::now() + budget;
    for n in 0..20 {
        s.start(spec(d.path(), n, "true")).unwrap();
    }
    for n in 0..20 {
        assert!(
            s.wait_finished(
                &format!("run-{n}"),
                deadline.saturating_duration_since(Instant::now())
            ),
            "run-{n} did not finish within the {budget:?} queue deadline"
        );
        assert_eq!(read(&s, n)["status"], "completed", "run-{n}");
    }
    assert!(s.max_held_seen() <= 3);
    assert_eq!(s.counts().total_started, 20);
    s.start(spec(d.path(), 20, "sleep .1; echo alive > survived"))
        .unwrap();
    drop(s);
    until(|| d.path().join("survived").exists());
    until(|| RunRecord::read(d.path(), "run-20").unwrap().to_value()["status"] == "completed");
}

#[test]
fn queue_full_counts_only_runs_that_would_wait() {
    // (held, reserved, max_runs, max_queued, full): a reserved run that a free
    // permit will promote is not waiting, so a burst of starts is not refused early.
    for (held, reserved, cap, queue, full) in [
        (0, 1, 2, 1, false),
        (0, 2, 1, 2, false),
        (1, 2, 1, 2, true),
        (0, 0, 1, 0, false),
        (0, 1, 1, 0, true),
        (1, 0, 1, 0, true),
    ] {
        assert_eq!(
            queue_full(held, reserved, cap, queue),
            full,
            "{held} {reserved} {cap} {queue}"
        );
    }
}

const HOLD: &str = "i=0; while [ ! -e go ] && [ $i -lt 400 ]; do sleep .01; i=$((i+1)); done";

#[test]
fn duplicate_run_preserves_live_and_finished_ownership() {
    let d = tempfile::tempdir().unwrap();
    let s = Supervisor::new(config(d.path(), 1, 0)).unwrap();
    s.start(spec(d.path(), 0, HOLD)).unwrap();
    until(|| read(&s, 0)["status"] == "running");
    let record = read(&s, 0);
    let counts = s.counts();
    assert!(matches!(
        s.start(spec(d.path(), 0, "exit 0")),
        Err(SupervisorError::DuplicateRun)
    ));
    assert_eq!(read(&s, 0), record);
    assert_eq!(s.counts(), counts);
    assert_eq!(s.cancel("run-0"), CancelOutcome::Requested);
    assert!(s.wait_finished("run-0", Duration::from_secs(1)));
    assert_eq!(read(&s, 0)["status"], "cancelled");
    let counts = s.counts();
    assert!(matches!(
        s.start(spec(d.path(), 0, "exit 0")),
        Err(SupervisorError::DuplicateRun)
    ));
    assert_eq!(s.counts(), counts);
}

#[test]
fn continuation_keeps_its_slot_ahead_of_queued_runs() {
    let d = tempfile::tempdir().unwrap();
    let s = Supervisor::new(config(d.path(), 1, 2)).unwrap();
    let next = spec(d.path(), 0, "echo substitute >> order");
    let mut first = gated_spec(d.path());
    first.finish = Some(Box::new(move |_, _| Some(next)));
    s.start(first).unwrap();
    until(|| s.counts().running == 1);
    s.start(spec(d.path(), 1, "echo queued >> order")).unwrap();
    until(|| s.counts().queued == 1);
    std::fs::write(d.path().join("go"), "").unwrap();
    assert!(s.wait_finished("run-0", Duration::from_secs(4)));
    assert!(s.wait_finished("run-1", Duration::from_secs(4)));
    assert_eq!(
        std::fs::read_to_string(d.path().join("order")).unwrap(),
        "substitute\nqueued\n"
    );
    assert_eq!(s.counts().total_started, 2);
}

#[test]
fn cancellation_during_finish_blocks_continuation_and_releases_slot() {
    let d = tempfile::tempdir().unwrap();
    let s = Supervisor::new(config(d.path(), 1, 2)).unwrap();
    let next = spec(d.path(), 0, "touch unexpected-substitute");
    let mut first = spec(
        d.path(),
        0,
        &format!("{HOLD}; printf 'HTTP 429: quota reached' >&2"),
    );
    first
        .base
        .insert("role".into(), serde_json::json!("review"));
    first.finish = Some(Box::new(move |record, _| {
        std::fs::write(record.path().with_file_name("cancelled"), "").unwrap();
        Some(next)
    }));
    s.start(first).unwrap();
    until(|| s.counts().running == 1);
    s.start(spec(d.path(), 1, "touch queued-finished")).unwrap();
    until(|| s.counts().queued == 1);
    std::fs::write(d.path().join("go"), "").unwrap();
    assert!(s.wait_finished("run-0", Duration::from_secs(4)));
    assert!(s.wait_finished("run-1", Duration::from_secs(4)));
    assert_eq!(read(&s, 0)["status"], "cancelled");
    assert!(!d.path().join("unexpected-substitute").exists());
    assert!(d.path().join("queued-finished").exists());
    assert_eq!(s.counts().running, 0);
}

fn review_slot(dir: &Path, name: &str) -> std::path::PathBuf {
    let slot = dir
        .join("cargo-target-pool")
        .join(format!("slot-review-{name}"));
    std::fs::create_dir_all(slot.join("target")).unwrap();
    std::fs::write(slot.join("target/object"), "build").unwrap();
    slot
}

#[cfg(target_os = "linux")]
#[test]
fn a_review_slot_stays_leased_while_its_run_lives_and_goes_with_it() {
    let d = tempfile::tempdir().unwrap();
    let slot = review_slot(d.path(), "seat");
    let s = Supervisor::new(config(d.path(), 1, 8)).unwrap();
    let mut run = spec(d.path(), 70, HOLD);
    run.review_slot = Some(slot.clone());
    s.start(run).unwrap();
    until(|| s.counts().running == 1);
    // Hygiene's exclusive lease on the slot is refused while the run is alive.
    assert!(crate::cargo_cache::try_exclusive_lease(&slot)
        .unwrap()
        .is_none());
    std::fs::write(d.path().join("go"), "").unwrap();
    assert!(s.wait_finished("run-70", Duration::from_secs(2)));
    until(|| !slot.exists());
    // The slot's name belongs to this run alone, so its lock file is removed with it.
    until(|| {
        !d.path()
            .join("cargo-target-pool/slot-review-seat.plangraph-lock")
            .exists()
    });
}

#[cfg(target_os = "linux")]
#[test]
fn a_slot_hygiene_is_reclaiming_is_refused_and_the_child_never_starts() {
    let d = tempfile::tempdir().unwrap();
    let slot = review_slot(d.path(), "reclaimed");
    let reclaiming = crate::cargo_cache::try_exclusive_lease(&slot)
        .unwrap()
        .unwrap();
    let s = Supervisor::new(config(d.path(), 1, 8)).unwrap();
    let mut run = spec(d.path(), 71, "touch child-ran");
    run.review_slot = Some(slot.clone());
    s.start(run).unwrap();
    assert!(s.wait_finished("run-71", Duration::from_secs(2)));
    let record = read(&s, 71);
    assert_eq!(record["status"], "failed", "{record}");
    assert!(
        record["reason"].as_str().unwrap().contains("review slot"),
        "{record}"
    );
    assert!(
        !d.path().join("child-ran").exists(),
        "the child must not start unleased"
    );
    drop(reclaiming);
}

#[cfg(target_os = "linux")]
#[test]
fn a_retry_that_switches_slots_leases_its_own_and_both_slots_go_with_the_run() {
    let d = tempfile::tempdir().unwrap();
    let (first_slot, second_slot) = (review_slot(d.path(), "one"), review_slot(d.path(), "two"));
    let s = Supervisor::new(config(d.path(), 1, 8)).unwrap();
    let mut second = spec(d.path(), 72, HOLD);
    second.review_slot = Some(second_slot.clone());
    let mut first = spec(d.path(), 72, "exit 0");
    first.review_slot = Some(first_slot.clone());
    first.finish = Some(Box::new(move |_, _| Some(second)));
    s.start(first).unwrap();
    until(|| {
        s.counts().running == 1
            && d.path()
                .join("cargo-target-pool/slot-review-two.plangraph-lock")
                .exists()
    });
    // The second leg runs in its own slot, which hygiene cannot take; the first leg's is free.
    assert!(crate::cargo_cache::try_exclusive_lease(&second_slot)
        .unwrap()
        .is_none());
    std::fs::write(d.path().join("go"), "").unwrap();
    assert!(s.wait_finished("run-72", Duration::from_secs(3)));
    until(|| !first_slot.exists() && !second_slot.exists());
}

#[cfg(target_os = "linux")]
#[test]
fn a_directory_the_run_did_not_allocate_is_never_leased_or_removed() {
    let d = tempfile::tempdir().unwrap();
    // CARGO_TARGET_DIR has the shape of a review slot's target, but no slot is declared owned.
    let slot = review_slot(d.path(), "foreign");
    let s = Supervisor::new(config(d.path(), 1, 8)).unwrap();
    let mut run = spec(d.path(), 73, "exit 0");
    run.env = vec![("CARGO_TARGET_DIR".into(), slot.join("target").into())];
    s.start(run).unwrap();
    assert!(s.wait_finished("run-73", Duration::from_secs(2)));
    assert!(slot.join("target/object").exists());
}

#[test]
fn only_a_review_slot_of_a_pool_is_recognized() {
    use crate::cargo_pool::is_review_slot;
    assert!(is_review_slot(Path::new(
        "/c/cargo-target-pool/slot-review-a"
    )));
    for other in [
        "/c/cargo-target-pool/slot-1",
        "/c/cargo-target/slot-review-a",
        "/c/cargo-target-pool/slot-review-a/target",
        "/c/cargo-target-pool",
        "/tmp/plain",
    ] {
        assert!(!is_review_slot(Path::new(other)), "{other}");
    }
}
