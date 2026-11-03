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
