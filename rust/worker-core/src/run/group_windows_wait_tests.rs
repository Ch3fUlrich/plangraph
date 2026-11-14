use std::{
    io::{BufRead, Write},
    process::{Child, Command},
    thread,
    time::{Duration, Instant},
};

pub(super) fn spawn_tree() -> ! {
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "run::group_windows_tests::fake_child",
            "--nocapture",
        ])
        .env("PG_FAKE_CHILD", "tree-leaf")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    // Creation has completed, including inherited job membership. launch() can
    // return before this helper's test harness has even reached fake_child().
    println!("tree-ready={}", child.id());
    std::io::stdout().flush().unwrap();
    // Neither process may time out naturally and mask a failed group stop.
    loop {
        thread::park();
    }
}

pub(super) fn tree_ready(child: &mut Child) -> (u32, thread::JoinHandle<()>) {
    let stdout = child.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    let output = thread::spawn(move || {
        for line in std::io::BufReader::new(stdout).lines() {
            let line = line.unwrap();
            if let Some((_, pid)) = line.split_once("tree-ready=") {
                tx.send(pid.parse::<u32>().unwrap()).unwrap();
            }
        }
    });
    // Only a hang guard: membership is asserted once, after the spawn signal.
    let pid = rx
        .recv_timeout(Duration::from_secs(30))
        .expect("helper did not signal grandchild creation");
    (pid, output)
}

pub(super) fn assert_stop_requires_kill(group: &dyn super::RunGroup, pids: &[u32]) {
    use super::{stop_group, Identity, RunGroup, StopOutcome};
    struct LeaveAlive<'a>(&'a dyn RunGroup);
    impl RunGroup for LeaveAlive<'_> {
        fn identity(&self) -> &Identity {
            self.0.identity()
        }
        fn members(&self) -> std::io::Result<Vec<u32>> {
            self.0.members()
        }
        fn terminate(&self) {}
        fn kill(&self) {}
    }
    // Failing-first variant: suppress both stop signals against the same real
    // job. Requiring Empty here would fail while its descendant survives.
    let StopOutcome::Survivors(survivors) =
        stop_group(&LeaveAlive(group), Duration::ZERO, Duration::ZERO)
    else {
        panic!("a stop that leaves the job alive must detect survivors");
    };
    assert_eq!(survivors.len(), pids.len());
    assert!(pids.iter().all(|pid| survivors.contains(pid)));
    assert!(pids.iter().all(|&pid| !super::gone(pid)));
}

pub(super) fn until_deadline(deadline: Instant, mut predicate: impl FnMut() -> bool) {
    loop {
        if predicate() {
            break;
        }
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn ready_condition_wins_over_elapsed_poll_deadline() {
    // A descheduled observer must still accept an already satisfied condition.
    until_deadline(Instant::now() - Duration::from_secs(1), || true);
}
