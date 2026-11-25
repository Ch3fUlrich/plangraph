use super::round_time_tests::{Transcript, MIN, T0};
use super::round_wait::TaskRef;
use super::round_watch::{blocked_outcome, tick, Action, Host, Owner, Stop};
use serde_json::json;
use std::{cell::RefCell, collections::HashSet, io};

pub(super) struct Fake {
    pub(super) owner: Option<Owner>,
    pub(super) stop: RefCell<Stop>,
    pub(super) fail_notify: bool,
    pub(super) asked: RefCell<Vec<String>>,
    pub(super) ended: RefCell<u32>,
    pub(super) undelivered: RefCell<bool>,
    pub(super) seen_signalled: RefCell<Vec<bool>>,
    pub(super) stopped: RefCell<Vec<(u32, String)>>,
    pub(super) sent: RefCell<Vec<(String, String, String)>>,
    /// Stop actions whose line the owner's output holds.
    pub(super) marks: RefCell<HashSet<String>>,
    pub(super) mark_ok: bool,
}

impl Fake {
    pub(super) fn new(owner: bool, stop: Stop) -> Self {
        Self {
            owner: owner.then_some(Owner {
                pid: 4242,
                start_time: Some(7),
            }),
            stop: RefCell::new(stop),
            fail_notify: false,
            asked: RefCell::default(),
            ended: RefCell::default(),
            undelivered: RefCell::new(false),
            seen_signalled: RefCell::default(),
            stopped: RefCell::default(),
            sent: RefCell::default(),
            marks: RefCell::default(),
            mark_ok: true,
        }
    }
    pub(super) fn live() -> Self {
        Self::new(true, Stop::Ended)
    }
}

impl Host for Fake {
    fn resolve_owner(&self, _: &str, task: &TaskRef) -> Option<Owner> {
        self.asked.borrow_mut().push(task.id.clone());
        self.owner.clone()
    }
    fn stop(&self, owner: &Owner, task: &TaskRef, signalled: bool) -> Stop {
        self.stopped.borrow_mut().push((owner.pid, task.id.clone()));
        self.seen_signalled.borrow_mut().push(signalled);
        *self.stop.borrow()
    }
    fn mark_stop(&self, _: &TaskRef, key: &str) -> bool {
        if self.mark_ok {
            self.marks.borrow_mut().insert(key.into());
        }
        self.mark_ok
    }
    fn has_mark(&self, _: &TaskRef, key: &str) -> bool {
        self.marks.borrow().contains(key)
    }
    fn pending(&self, _: &str, _: &str) -> bool {
        *self.undelivered.borrow()
    }
    fn end_session(&self, _: &str) -> bool {
        *self.ended.borrow_mut() += 1;
        true
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
pub(super) fn waiting(task: &str, start: i64, t: &mut Transcript) {
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
        "TaskOutput",
        json!({"task_id": task, "block": true}),
    );
}

pub(super) fn first_wait(task: &str) -> Transcript {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    waiting(task, 0, &mut t);
    t
}

pub(super) fn run(t: &Transcript, at_min: i64, dir: &std::path::Path, host: &Fake) -> Vec<Action> {
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
    assert_eq!(*host.stopped.borrow(), vec![(4242, "a".to_string())]);
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
    *host.undelivered.borrow_mut() = true; // the bridge has not flushed the BLOCKED notices yet
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
    // Not stopped while its own BLOCKED notices wait in the outbox: the writer must read why.
    assert!(
        !second
            .iter()
            .any(|a| matches!(a, Action::RoundEnded { .. })),
        "{second:?}"
    );
    assert!(run(&t, 35, dir.path(), &host).is_empty());
    assert_eq!(*host.ended.borrow(), 0);
    *host.undelivered.borrow_mut() = false;
    let after = run(&t, 36, dir.path(), &host);
    assert!(
        after.contains(&Action::RoundEnded {
            round_key: "writer-1:p1".into()
        }),
        "{after:?}"
    );
    assert_eq!(*host.ended.borrow(), 1, "the live session is stopped once");
    assert_eq!(
        blocked_outcome(&dir.path().join("w.json")).unwrap()["closed_ms"],
        T0 + 34 * MIN
    );
    // Owners are asked for by task id, never by command text.
    assert_eq!(host.asked.borrow().iter().filter(|t| *t == "a").count(), 1);
    assert!(run(&t, 40, dir.path(), &host).is_empty());
    assert_eq!(*host.ended.borrow(), 1);
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
fn a_retry_after_survivors_tells_the_host_a_signal_was_sent() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Survivors);
    let t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    run(&t, 16, dir.path(), &host);
    // The host decides: with the earlier signal on record, an empty group is a confirmed stop.
    let restarted = Fake::new(false, Stop::Ended);
    let done = run(&t, 17, dir.path(), &restarted);
    assert!(done.contains(&Action::Stopped {
        task_id: "a".into(),
        command: "cargo test -p a".into()
    }));
    assert_eq!(*restarted.seen_signalled.borrow(), vec![true]);
    assert_eq!(
        *host.seen_signalled.borrow(),
        vec![false],
        "the first stop had no signal on record"
    );
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

#[test]
fn a_crash_before_the_signal_and_a_natural_exit_is_no_strike() {
    let dir = tempfile::tempdir().unwrap();
    let t = first_wait("a");
    let host = Fake::new(true, Stop::Gone);
    run(&t, 1, dir.path(), &host);
    // The watchdog wrote its stop and died before it signalled anything.
    let path = dir.path().join("w.json");
    let mut state: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    state["actions"]["writer-1:p1|a|w-a"] = json!({"phase": "stopping", "round_key": "writer-1:p1",
        "task_id": "a", "call_id": "w-a", "command": "cargo test -p a", "waited_ms": 1,
        "task": {"id": "a", "output_path": "/t/tasks/a.output", "launched_from_ms": 0, "launched_to_ms": 1}});
    std::fs::write(&path, state.to_string()).unwrap();
    let done = run(&t, 16, dir.path(), &host);
    assert_eq!(
        done,
        vec![Action::Finished {
            task_id: "a".into()
        }]
    );
    assert!(host.sent.borrow().is_empty());
    assert_eq!(*host.ended.borrow(), 0);
}

#[test]
fn a_stop_needs_the_very_wait_call_that_earned_it() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Survivors);
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    assert_eq!(run(&t, 16, dir.path(), &host).len(), 1);
    // The first wait call returns; the writer opens a new one on the same, still running task.
    t.result(16 * MIN + 10_000, "w-a", None);
    // Other work in between ends the wait episode: the next call starts a new one.
    t.request(16 * MIN + 10_200);
    t.tool(
        16 * MIN + 10_400,
        "e1",
        "Edit",
        json!({"file_path": "a.rs"}),
    );
    t.result(16 * MIN + 10_600, "e1", None);
    t.request(16 * MIN + 11_000);
    t.tool(
        16 * MIN + 12_000,
        "w2-a",
        "TaskOutput",
        json!({"task_id": "a", "block": true}),
    );
    let retry = Fake::new(true, Stop::Ended);
    assert!(run(&t, 17, dir.path(), &retry).is_empty());
    assert!(
        retry.stopped.borrow().is_empty(),
        "the new call has not earned a stop"
    );
}

#[test]
fn a_wait_on_a_prefix_task_id_stops_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    waiting("bg1", 0, &mut t);
    // The writer's call names bg10, which is not running.
    t.0.pop();
    t.tool(
        6_000,
        "w-x",
        "TaskOutput",
        json!({"task_id": "bg10", "block": true}),
    );
    run(&t, 1, dir.path(), &host);
    assert!(run(&t, 40, dir.path(), &host).is_empty());
    assert!(host.stopped.borrow().is_empty());
}

#[test]
fn a_replacement_wait_call_qualifies_on_its_own_after_a_release() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Survivors);
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    assert_eq!(run(&t, 16, dir.path(), &host).len(), 1);
    // The first call returns; the writer opens a second call on the same running task.
    t.result(16 * MIN + 10_000, "w-a", None);
    // Other work in between ends the wait episode: the next call starts a new one.
    t.request(16 * MIN + 10_200);
    t.tool(
        16 * MIN + 10_400,
        "e1",
        "Edit",
        json!({"file_path": "a.rs"}),
    );
    t.result(16 * MIN + 10_600, "e1", None);
    t.request(16 * MIN + 11_000);
    t.tool(
        16 * MIN + 12_000,
        "w2-a",
        "TaskOutput",
        json!({"task_id": "a", "block": true}),
    );
    let after = Fake::new(true, Stop::Ended);
    assert!(
        run(&t, 17, dir.path(), &after).is_empty(),
        "the old action is released"
    );
    assert!(after.stopped.borrow().is_empty());
    // Fifteen minutes after its own start the second call earns its own stop.
    assert!(run(&t, 30, dir.path(), &after).is_empty(), "14 minutes old");
    let done = run(&t, 32, dir.path(), &after);
    assert!(
        done.contains(&Action::Stopped {
            task_id: "a".into(),
            command: "cargo test -p a".into()
        }),
        "{done:?}"
    );
}

#[test]
fn a_shell_wait_that_names_the_output_file_is_never_stopped() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    waiting("a", 0, &mut t);
    t.0.pop();
    t.tool(
        6_000,
        "w-sh",
        "Bash",
        json!({"command":"until grep -q rc= /t/tasks/a.output; do sleep 15; done"}),
    );
    run(&t, 1, dir.path(), &host);
    assert!(run(&t, 60, dir.path(), &host).is_empty());
    assert!(
        host.stopped.borrow().is_empty(),
        "nothing proves the loop waits on it"
    );
}

#[test]
fn a_non_blocking_task_output_call_is_never_stopped() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    waiting("a", 0, &mut t);
    t.0.pop();
    t.tool(
        6_000,
        "w-poll",
        "TaskOutput",
        json!({"task_id": "a", "block": false}),
    );
    run(&t, 1, dir.path(), &host);
    assert!(run(&t, 60, dir.path(), &host).is_empty());
    assert!(host.stopped.borrow().is_empty());
}
