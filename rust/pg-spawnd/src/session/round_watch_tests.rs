use super::round_time_tests::{Transcript, MIN, T0};
use super::round_watch::{blocked_outcome, tick, Action, Host, Owner, Stop};
use serde_json::json;
use std::{cell::RefCell, io};

struct Fake {
    owner: Option<Owner>,
    stop: RefCell<Stop>,
    fail_notify: bool,
    stopped: RefCell<Vec<(u32, String)>>,
    sent: RefCell<Vec<(String, String, String)>>,
}

impl Fake {
    fn new(owner: bool, stop: Stop) -> Self {
        Self {
            owner: owner.then_some(Owner {
                pid: 4242,
                start_time: Some(7),
            }),
            stop: RefCell::new(stop),
            fail_notify: false,
            stopped: RefCell::default(),
            sent: RefCell::default(),
        }
    }
    fn live() -> Self {
        Self::new(true, Stop::Ended)
    }
}

impl Host for Fake {
    fn resolve_owner(&self, _: &str, _: &str) -> Option<Owner> {
        self.owner.clone()
    }
    fn stop(&self, owner: &Owner, command: &str) -> Stop {
        self.stopped.borrow_mut().push((owner.pid, command.into()));
        *self.stop.borrow()
    }
    fn notify(&self, recipient: &str, key: &str, line: &str) -> io::Result<()> {
        if self.fail_notify {
            return Err(io::Error::other("outbox down"));
        }
        let mut sent = self.sent.borrow_mut();
        if !sent.iter().any(|(r, k, _)| r == recipient && k == key) {
            sent.push((recipient.into(), key.into(), line.into()));
        }
        Ok(())
    }
}

/// Background `cargo test -p <task>` started at `start`; the writer then waits on its output.
fn waiting(task: &str, start: i64, t: &mut Transcript) {
    t.request(start + 1_000);
    t.tool(
        start + 3_000,
        &format!("u-{task}"),
        "Bash",
        json!({"command":format!("cargo test -p {task}"),"run_in_background":true}),
    );
    t.result(start + 3_500, &format!("u-{task}"), Some(task));
    t.request(start + 4_000);
    t.tool(
        start + 6_000,
        &format!("w-{task}"),
        "Bash",
        json!({"command":format!("until grep -q rc= /t/tasks/{task}.output; do sleep 15; done")}),
    );
}

fn first_wait(task: &str) -> Transcript {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    waiting(task, 0, &mut t);
    t
}

fn run(t: &Transcript, at_min: i64, dir: &std::path::Path, host: &Fake) -> Vec<Action> {
    let text: Vec<String> = t.0.iter().map(|v| v.to_string()).collect();
    tick(
        "writer-1",
        &text.join("\n"),
        T0 + at_min * MIN,
        &dir.join("w.json"),
        host,
    )
    .unwrap()
}

#[test]
fn watchdog_fires_during_silence_and_blocks_on_second_wait() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    // The transcript is silent after the wait call; only the clock moves.
    assert!(
        run(&t, 1, dir.path(), &host).is_empty(),
        "owner recorded, nothing to do"
    );
    assert!(run(&t, 14, dir.path(), &host).is_empty(), "under the cap");
    let first = run(&t, 16, dir.path(), &host);
    assert_eq!(
        first,
        vec![
            Action::Stopped {
                task_id: "a".into(),
                command: "cargo test -p a".into()
            },
            Action::Notified {
                task_id: "a".into()
            },
        ]
    );
    assert_eq!(
        *host.stopped.borrow(),
        vec![(4242, "cargo test -p a".to_string())]
    );
    {
        let sent = host.sent.borrow();
        assert_eq!(sent.len(), 1);
        assert!(sent[0].2.contains("cargo test -p a") && sent[0].2.contains("15 min"));
    }
    // A later tick and a restart on the same state file add nothing.
    assert!(run(&t, 17, dir.path(), &host).is_empty());
    assert!(run(&t, 18, dir.path(), &Fake::live()).is_empty());
    assert_eq!(host.sent.borrow().len(), 1);
    assert_eq!(blocked_outcome(&dir.path().join("w.json")), None);

    // The harness reports task a ended and the wait call returns; the writer waits on b.
    t.notified(17 * MIN, "a");
    t.result(17 * MIN, "w-a", None);
    waiting("b", 17 * MIN, &mut t);
    assert!(
        run(&t, 20, dir.path(), &host).is_empty(),
        "owner of b recorded"
    );
    assert!(run(&t, 30, dir.path(), &host).is_empty(), "b under the cap");
    let second = run(&t, 34, dir.path(), &host);
    assert!(
        second.contains(&Action::Blocked {
            round_key: "writer-1:p1".into(),
            command: "cargo test -p b".into()
        }),
        "{second:?}"
    );
    let outcome = blocked_outcome(&dir.path().join("w.json")).unwrap();
    assert_eq!(outcome["command"], "cargo test -p b");
    let sent = host.sent.borrow();
    let to: Vec<&str> = sent.iter().map(|(who, _, _)| who.as_str()).collect();
    assert_eq!(to, ["writer-1", "writer-1", "routing"]);
    assert!(sent[2].2.starts_with("BLOCKED"));
    drop(sent);
    assert!(run(&t, 40, dir.path(), &host).is_empty());
}

#[test]
fn the_blocked_record_exists_even_when_the_alert_cannot_be_queued() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    run(&t, 16, dir.path(), &host);
    t.notified(17 * MIN, "a");
    t.result(17 * MIN, "w-a", None);
    waiting("b", 17 * MIN, &mut t);
    run(&t, 20, dir.path(), &host);
    host.fail_notify = true;
    run(&t, 34, dir.path(), &host);
    assert_eq!(
        blocked_outcome(&dir.path().join("w.json")).unwrap()["command"],
        "cargo test -p b"
    );
}

#[test]
fn a_stop_that_leaves_survivors_is_no_strike_and_the_recorded_owner_is_stopped_after_restart() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Survivors);
    let t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    assert_eq!(
        run(&t, 16, dir.path(), &host),
        vec![Action::StopPending {
            task_id: "a".into()
        }]
    );
    assert!(host.sent.borrow().is_empty());
    // After a restart nothing can be resolved any more: the persisted record decides.
    let restarted = Fake::new(false, Stop::Ended);
    let done = run(&t, 17, dir.path(), &restarted);
    assert_eq!(done.len(), 2, "{done:?}");
    assert_eq!(restarted.stopped.borrow()[0].0, 4242);
    assert_eq!(restarted.sent.borrow().len(), 1);
}

#[test]
fn an_owner_gone_after_our_signal_is_a_confirmed_stop() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Survivors);
    let t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    run(&t, 16, dir.path(), &host);
    // Crash after the signal: the next tick finds nothing left of the process.
    let restarted = Fake::new(false, Stop::Gone);
    let done = run(&t, 17, dir.path(), &restarted);
    assert!(done.contains(&Action::Stopped {
        task_id: "a".into(),
        command: "cargo test -p a".into()
    }));
    assert_eq!(restarted.sent.borrow().len(), 1);
}

#[test]
fn a_command_that_finished_by_itself_before_the_stop_is_no_strike_and_no_notice() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Gone);
    let t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    let done = run(&t, 16, dir.path(), &host);
    assert_eq!(
        done,
        vec![Action::Finished {
            task_id: "a".into()
        }]
    );
    assert!(
        host.sent.borrow().is_empty(),
        "no claim that the watchdog ended it"
    );
    assert!(run(&t, 17, dir.path(), &host).is_empty());
}

#[test]
fn without_a_recorded_owner_nothing_is_signalled() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(false, Stop::Ended);
    let t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    let done = run(&t, 16, dir.path(), &host);
    assert_eq!(
        done,
        vec![Action::OwnerUnknown {
            task_id: "a".into()
        }]
    );
    assert!(host.stopped.borrow().is_empty());
    assert!(host.sent.borrow().is_empty());
}

#[test]
fn an_idle_writer_with_a_running_command_is_never_stopped() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u1",
        "Bash",
        json!({"command":"cargo test -p a","run_in_background":true}),
    );
    t.result(2_500, "u1", Some("a"));
    t.request(3_000);
    t.text(4_000, true);
    run(&t, 1, dir.path(), &host);
    assert!(run(&t, 60, dir.path(), &host).is_empty());
    assert!(host.stopped.borrow().is_empty());
}

#[test]
fn a_pending_stop_is_released_when_its_wait_closed_before_the_stop() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Survivors);
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    assert_eq!(run(&t, 16, dir.path(), &host).len(), 1);
    // The command ends by itself and the harness reports it before the stop is retried.
    t.notified(17 * MIN, "a");
    t.result(17 * MIN, "w-a", None);
    let retry = Fake::live();
    assert!(run(&t, 18, dir.path(), &retry).is_empty());
    assert!(
        retry.stopped.borrow().is_empty(),
        "no signal without an open wait"
    );
    assert!(retry.sent.borrow().is_empty());
    let state = std::fs::read_to_string(dir.path().join("w.json")).unwrap();
    assert!(state.contains("released"));
}
