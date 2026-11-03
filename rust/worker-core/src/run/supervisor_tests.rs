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
