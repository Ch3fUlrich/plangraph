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
    // Ensure the first shell has recorded its launch before filling the second slot:
    // permit FIFO cannot impose scheduling order on two concurrently launched shells.
    s.start(spec(d.path(), 0, "echo 0 >> order; sleep .2"))
        .unwrap();
    until(|| d.path().join("order").exists());
    for n in 1..5 {
        s.start(spec(d.path(), n, &format!("echo {n} >> order; sleep .2")))
            .unwrap();
    }
    until(|| s.counts().running == 2);
    assert_eq!(s.counts().queued, 3);
    until(|| {
        assert!(s.counts().running <= 2);
        s.counts().running == 0 && s.counts().queued == 0
    });
    for n in 0..5 {
        assert!(s.wait_finished(&format!("run-{n}"), Duration::from_secs(1)));
        assert_eq!(read(&s, n)["status"], "completed");
    }
    assert_eq!(
        std::fs::read_to_string(d.path().join("order")).unwrap(),
        "0\n1\n2\n3\n4\n"
    );
    assert_eq!(s.max_held_seen(), 2);
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
    let began = Instant::now();
    for n in 0..20 {
        s.start(spec(d.path(), n, "true")).unwrap();
    }
    for n in 0..20 {
        assert!(s.wait_finished(&format!("run-{n}"), Duration::from_secs(2)));
    }
    assert!(began.elapsed() < Duration::from_secs(5));
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
