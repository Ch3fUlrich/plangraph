use super::round_time_tests::{Transcript, MIN, T0};
use super::round_watch::{blocked_outcome, tick, Action, Host, Owner};
use serde_json::json;
use std::{cell::RefCell, io};

#[derive(Default)]
struct Fake {
    owner: RefCell<Option<Owner>>,
    stop_ok: RefCell<bool>,
    stopped: RefCell<Vec<u32>>,
    sent: RefCell<Vec<(String, String, String)>>,
}

impl Fake {
    fn with_owner() -> Self {
        let fake = Self::default();
        *fake.owner.borrow_mut() = Some(Owner {
            pid: 4242,
            start_time: Some(7),
        });
        *fake.stop_ok.borrow_mut() = true;
        fake
    }
}

impl Host for Fake {
    fn resolve_owner(&self, _: &str, _: &str) -> Option<Owner> {
        self.owner.borrow().clone()
    }
    fn stop(&self, owner: &Owner) -> bool {
        self.stopped.borrow_mut().push(owner.pid);
        *self.stop_ok.borrow()
    }
    fn notify(&self, recipient: &str, key: &str, line: &str) -> io::Result<()> {
        let mut sent = self.sent.borrow_mut();
        if !sent.iter().any(|(r, k, _)| r == recipient && k == key) {
            sent.push((recipient.into(), key.into(), line.into()));
        }
        Ok(())
    }
}

fn waiting(task: &str, start: i64) -> Transcript {
    let mut t = Transcript::new();
    t.prompt(start, "p1");
    t.request(start + 1_000);
    t.tool(
        start + 3_000,
        &format!("u-{task}"),
        "Bash",
        json!({"command":format!("cargo test -p {task}"),"run_in_background":true}),
    );
    t.result(start + 3_500, &format!("u-{task}"), Some(task));
    t.request(start + 4_000);
    t.text(start + 6_000, true);
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
    let host = Fake::with_owner();
    let mut t = waiting("a", 0);
    // The transcript is silent: no line after 6 s, the clock alone moves.
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
    assert_eq!(*host.stopped.borrow(), vec![4242]);
    {
        let sent = host.sent.borrow();
        assert_eq!(sent.len(), 1);
        assert!(sent[0].2.contains("cargo test -p a") && sent[0].2.contains("15 min"));
    }
    // A later tick and a restart on the same state file add nothing.
    assert!(run(&t, 17, dir.path(), &host).is_empty());
    assert!(run(&t, 18, dir.path(), &Fake::with_owner()).is_empty());
    assert_eq!(host.sent.borrow().len(), 1);
    assert_eq!(blocked_outcome(&dir.path().join("w.json")), None);

    // The harness reports the end of task a; the writer starts task b and waits again.
    t.notified(17 * MIN, "a");
    t.request(17 * MIN + 500);
    t.tool(
        17 * MIN + 1_000,
        "u-b",
        "Bash",
        json!({"command":"cargo test -p b","run_in_background":true}),
    );
    t.result(17 * MIN + 1_500, "u-b", Some("b"));
    t.request(17 * MIN + 2_000);
    t.text(17 * MIN + 3_000, true);
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
fn a_stop_that_leaves_survivors_is_no_strike_and_is_retried_after_restart() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::with_owner();
    *host.stop_ok.borrow_mut() = false;
    let t = waiting("a", 0);
    assert_eq!(
        run(&t, 16, dir.path(), &host),
        vec![Action::StopPending {
            task_id: "a".into()
        }]
    );
    assert!(host.sent.borrow().is_empty());
    let state = std::fs::read_to_string(dir.path().join("w.json")).unwrap();
    assert!(state.contains("stopping") && !state.contains("\"stopped\""));
    let restarted = Fake::with_owner();
    let done = run(&t, 17, dir.path(), &restarted);
    assert_eq!(done.len(), 2);
    assert_eq!(restarted.sent.borrow().len(), 1);
}

#[test]
fn a_command_that_finished_before_the_stop_needs_no_signal() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::default();
    let t = waiting("a", 0);
    let done = run(&t, 16, dir.path(), &host);
    assert!(host.stopped.borrow().is_empty());
    assert_eq!(done.len(), 2);
}

#[test]
fn a_wait_that_ended_in_time_is_never_stopped() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::with_owner();
    let mut t = waiting("a", 0);
    t.notified(10 * MIN, "a");
    assert!(run(&t, 40, dir.path(), &host).is_empty());
    assert!(host.stopped.borrow().is_empty());
}

#[test]
fn a_pending_stop_is_released_when_its_wait_closed_before_the_stop() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::with_owner();
    *host.stop_ok.borrow_mut() = false;
    let mut t = waiting("a", 0);
    assert_eq!(run(&t, 16, dir.path(), &host).len(), 1);
    // The command ends by itself and the harness reports it before the stop is retried.
    t.notified(17 * MIN, "a");
    let retry = Fake::with_owner();
    assert!(run(&t, 18, dir.path(), &retry).is_empty());
    assert!(
        retry.stopped.borrow().is_empty(),
        "no signal without an open wait"
    );
    assert!(retry.sent.borrow().is_empty());
    let state = std::fs::read_to_string(dir.path().join("w.json")).unwrap();
    assert!(state.contains("released"));
}
