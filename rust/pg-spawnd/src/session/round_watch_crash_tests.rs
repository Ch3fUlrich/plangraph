//! Crash, receipt and delivery-outage cases of the round watchdog.

use super::round_time_tests::{Transcript, MIN, T0};
use super::round_watch::{blocked_outcome, Action, Stop};
use super::round_watch_tests::{first_wait, run, waiting, Fake};
use serde_json::json;

const KEY: &str = "writer-1:p1|a|w-a";

/// The state a watchdog leaves behind when it dies after marking the owner's output (and,
/// with `acked`, after the kernel accepted its signal and that was written down).
fn crashed(dir: &std::path::Path, acked: bool) {
    let path = dir.join("w.json");
    let mut state: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    let mut action = json!({"phase": "stopping", "marked": true,
        "round_key": "writer-1:p1", "task_id": "a", "call_id": "w-a",
        "command": "cargo test -p a", "waited_ms": 1,
        "task": {"id": "a", "output_path": "/t/tasks/a.output",
                 "launched_from_ms": 0, "launched_to_ms": 1}});
    if acked {
        action["acked"] = json!(true);
        action["acked_ms"] = json!(T0 + 15 * MIN);
    }
    state["actions"][KEY] = action;
    std::fs::write(&path, state.to_string()).unwrap();
}

/// The wait call returns and the harness reports the task ended with `code`.
fn ended_by(t: &mut Transcript, code: i64) {
    t.result(16 * MIN, "w-a", None);
    t.0.push(json!({"type":"user","timestamp": super::round_time_tests::stamp_from(T0, 16 * MIN),
        "origin":{"kind":"task-notification"},
        "message":{"role":"user","content": format!(
            "<task-notification><task-id>a</task-id><status>failed</status><summary>Background command \"cargo test -p a\" failed with exit code {code}</summary></task-notification>")}}));
}

#[test]
fn a_delivery_outage_does_not_keep_a_blocked_writer_running() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    run(&t, 16, dir.path(), &host);
    t.notified(17 * MIN, "a");
    t.result(17 * MIN, "w-a", None);
    waiting("b", 17 * MIN, &mut t);
    run(&t, 20, dir.path(), &host);
    *host.undelivered.borrow_mut() = true; // the bridge is down and never flushes
    assert!(run(&t, 34, dir.path(), &host)
        .iter()
        .any(|a| matches!(a, Action::Blocked { .. })));
    assert!(
        run(&t, 40, dir.path(), &host).is_empty(),
        "inside the grace the writer may still read why"
    );
    assert_eq!(*host.ended.borrow(), 0);
    let late = run(&t, 45, dir.path(), &host);
    assert!(
        late.contains(&Action::NoticesOverdue {
            round_key: "writer-1:p1".into()
        }),
        "{late:?}"
    );
    assert!(
        late.contains(&Action::RoundEnded {
            round_key: "writer-1:p1".into()
        }),
        "{late:?}"
    );
    assert_eq!(*host.ended.borrow(), 1);
    assert_eq!(
        host.sent.borrow().len(),
        3,
        "the notices stay queued for the bridge"
    );
}

#[test]
fn no_signal_is_sent_when_the_owners_output_cannot_be_marked() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = Fake::live();
    host.mark_ok = false;
    let t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    let done = run(&t, 16, dir.path(), &host);
    assert_eq!(
        done,
        vec![Action::StopPending {
            task_id: "a".into()
        }]
    );
    assert!(host.stopped.borrow().is_empty());
}

#[test]
fn a_blocked_round_whose_notices_cannot_be_queued_still_ends_after_the_grace_from_the_block() {
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
    let blocked = run(&t, 34, dir.path(), &host);
    assert!(
        blocked.iter().any(|a| matches!(a, Action::Blocked { .. })),
        "{blocked:?}"
    );
    assert_eq!(*host.ended.borrow(), 0);
    let late = run(&t, 45, dir.path(), &host);
    assert!(
        late.contains(&Action::RoundEnded {
            round_key: "writer-1:p1".into()
        }),
        "{late:?}"
    );
    assert_eq!(*host.ended.borrow(), 1);
    // The notice is retried on its own and reaches the outbox once the bridge works again.
    host.fail_notify = false;
    run(&t, 46, dir.path(), &host);
    assert!(host
        .sent
        .borrow()
        .iter()
        .any(|(who, _, text)| who == "routing" && text.starts_with("BLOCKED")));
}

#[test]
fn the_blocked_record_holds_the_command_redacted() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    for (task, command) in [
        ("a", "cargo test -p a"),
        ("b", "deploy API_TOKEN=hunter2hunter2hunter2"),
    ] {
        t.request(1_000);
        t.tool(
            2_000,
            &format!("u-{task}"),
            "Bash",
            json!({"command": command, "run_in_background": true}),
        );
        t.result(2_500, &format!("u-{task}"), Some(task));
    }
    t.request(3_000);
    t.tool(
        4_000,
        "w-a",
        "TaskOutput",
        json!({"task_id": "a", "block": true}),
    );
    run(&t, 1, dir.path(), &host);
    run(&t, 16, dir.path(), &host);
    t.notified(17 * MIN, "a");
    t.result(17 * MIN, "w-a", None);
    t.request(17 * MIN + 500);
    t.tool(
        17 * MIN + 1_000,
        "w-b",
        "TaskOutput",
        json!({"task_id": "b", "block": true}),
    );
    run(&t, 18, dir.path(), &host);
    run(&t, 34, dir.path(), &host);
    let blocked = blocked_outcome(&dir.path().join("w.json")).unwrap();
    assert!(!blocked.to_string().contains("hunter2"), "{blocked}");
    assert!(blocked["command"].as_str().unwrap().contains("deploy"));
}

#[test]
fn back_to_back_blocking_calls_are_one_wait_that_the_cap_applies_to() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u-a",
        "Bash",
        json!({"command":"cargo test -p a","run_in_background":true}),
    );
    t.result(2_500, "u-a", Some("a"));
    // A task-output call blocks at most five minutes: the writer calls again and again.
    for (n, at) in [
        10_000,
        5 * MIN + 10_000,
        10 * MIN + 10_000,
        15 * MIN + 10_000,
    ]
    .into_iter()
    .enumerate()
    {
        t.request(at - 500);
        t.tool(
            at,
            &format!("w{n}"),
            "TaskOutput",
            json!({"task_id": "a", "block": true, "timeout": 300_000}),
        );
        if n < 3 {
            t.result(at + 5 * MIN - 1_000, &format!("w{n}"), None);
        }
    }
    run(&t, 1, dir.path(), &host);
    // 15 minutes after the first call, in the fourth call: over the cap, as one wait.
    let done = run(&t, 16, dir.path(), &host);
    assert!(
        done.contains(&Action::Stopped {
            task_id: "a".into(),
            command: "cargo test -p a".into()
        }),
        "{done:?}"
    );
    assert_eq!(
        host.stopped.borrow().len(),
        1,
        "one stop for the whole chain"
    );
}

#[test]
fn a_stop_pending_between_two_calls_of_one_wait_signals_nothing_until_a_call_is_open() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Survivors);
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    assert_eq!(run(&t, 16, dir.path(), &host).len(), 1);
    // The call returned and the next is not issued yet when the watchdog looks.
    t.result(16 * MIN + 50_000, "w-a", None);
    let retry = Fake::new(true, Stop::Ended);
    retry.marks.borrow_mut().insert("writer-1:p1|a|w-a".into());
    assert!(
        run(&t, 17, dir.path(), &retry).is_empty(),
        "nothing is signalled between calls"
    );
    assert!(retry.stopped.borrow().is_empty());
    assert!(!std::fs::read_to_string(dir.path().join("w.json"))
        .unwrap()
        .contains("released"));
    // The next call of the chain opens: only now may the stop go on.
    t.request(16 * MIN + 54_000);
    t.tool(
        16 * MIN + 55_000,
        "w2-a",
        "TaskOutput",
        json!({"task_id": "a", "block": true}),
    );
    let done = run(&t, 17, dir.path(), &retry);
    assert!(
        done.contains(&Action::Stopped {
            task_id: "a".into(),
            command: "cargo test -p a".into()
        }),
        "{done:?}"
    );
    assert!(
        retry.stopped.borrow().is_empty(),
        "the accepted signal is not sent again"
    );
    assert_eq!(retry.finishes.borrow().len(), 1);
}

#[test]
fn a_crash_after_the_accepted_signal_was_written_down_is_a_stop_once_the_group_is_gone() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    crashed(dir.path(), true);
    t.result(16 * MIN, "w-a", None);
    let restarted = Fake::live();
    let done = run(&t, 17, dir.path(), &restarted);
    assert!(
        done.contains(&Action::Stopped {
            task_id: "a".into(),
            command: "cargo test -p a".into()
        }),
        "{done:?}"
    );
    assert!(
        done.contains(&Action::Notified {
            task_id: "a".into()
        }),
        "{done:?}"
    );
    assert!(restarted.stopped.borrow().is_empty(), "no new signal");
    assert_eq!(restarted.sent.borrow().len(), 1, "the nudge is not lost");
}

#[test]
fn without_a_written_acknowledgement_no_exit_code_is_a_strike() {
    // The watchdog died after the mark and before, or while, it signalled. 129 is what
    // SIGHUP and a plain `exit 129` both give; 143 and 137 are what other ends give.
    for code in [129, 143, 137, 0] {
        let dir = tempfile::tempdir().unwrap();
        let host = Fake::live();
        let mut t = first_wait("a");
        run(&t, 1, dir.path(), &host);
        crashed(dir.path(), false);
        ended_by(&mut t, code);
        let restarted = Fake::live();
        let done = run(&t, 17, dir.path(), &restarted);
        assert!(done.is_empty(), "exit {code}: {done:?}");
        assert!(restarted.sent.borrow().is_empty(), "exit {code}: no nudge");
        assert!(
            restarted.stopped.borrow().is_empty(),
            "exit {code}: no signal"
        );
        let state = std::fs::read_to_string(dir.path().join("w.json")).unwrap();
        assert!(state.contains("released"), "exit {code}");
    }
}

#[test]
fn an_acknowledged_stop_whose_group_still_runs_stays_pending_then_is_released_after_the_grace() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    crashed(dir.path(), true);
    t.result(16 * MIN, "w-a", None);
    let restarted = Fake::live();
    *restarted.gone.borrow_mut() = false;
    assert!(run(&t, 17, dir.path(), &restarted).is_empty());
    assert!(
        restarted.stopped.borrow().is_empty(),
        "no signal once the wait is over"
    );
    assert!(!std::fs::read_to_string(dir.path().join("w.json"))
        .unwrap()
        .contains("released"));
    // The group goes away: the acknowledged stop is a stop.
    *restarted.gone.borrow_mut() = true;
    let done = run(&t, 18, dir.path(), &restarted);
    assert!(
        done.contains(&Action::Stopped {
            task_id: "a".into(),
            command: "cargo test -p a".into()
        }),
        "{done:?}"
    );
    // A group that never goes away is released after the grace, without a strike.
    let dir2 = tempfile::tempdir().unwrap();
    let mut t2 = first_wait("a");
    run(&t2, 1, dir2.path(), &host);
    crashed(dir2.path(), true);
    t2.result(16 * MIN, "w-a", None);
    let stuck = Fake::live();
    *stuck.gone.borrow_mut() = false;
    assert!(run(&t2, 17, dir2.path(), &stuck).is_empty());
    assert!(run(&t2, 16 + 11, dir2.path(), &stuck).is_empty());
    assert!(std::fs::read_to_string(dir2.path().join("w.json"))
        .unwrap()
        .contains("released"));
    assert!(stuck.sent.borrow().is_empty());
}

#[test]
fn a_stop_that_reached_no_member_records_no_acknowledgement_and_a_later_natural_exit_is_no_strike()
{
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::new(true, Stop::Unsignalled);
    let t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    assert_eq!(
        run(&t, 16, dir.path(), &host),
        vec![Action::StopPending {
            task_id: "a".into()
        }]
    );
    assert!(!std::fs::read_to_string(dir.path().join("w.json"))
        .unwrap()
        .contains("\"acked\":true"));
    // The command now ends by itself: nothing is left to signal and nothing is on record.
    let later = Fake::new(true, Stop::Gone);
    assert_eq!(
        run(&t, 17, dir.path(), &later),
        vec![Action::Finished {
            task_id: "a".into()
        }]
    );
    assert!(later.sent.borrow().is_empty());
}

#[test]
fn overlapping_calls_alone_never_reach_the_cap() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u-a",
        "Bash",
        json!({"command":"cargo test -p a","run_in_background":true}),
    );
    t.result(2_500, "u-a", Some("a"));
    // Three five-minute calls, two of them overlapping almost fully.
    for (n, at) in [10_000, 12_000, 5 * MIN + 13_000].into_iter().enumerate() {
        t.request(at - 500);
        t.tool(
            at,
            &format!("w{n}"),
            "TaskOutput",
            json!({"task_id": "a", "block": true}),
        );
    }
    t.result(10_000 + 5 * MIN, "w0", None);
    t.result(12_000 + 5 * MIN, "w1", None);
    run(&t, 1, dir.path(), &host);
    // The calls add up to 15.8 minutes, the time spent waiting is 10.8: no stop.
    assert!(
        run(&t, 11, dir.path(), &host).is_empty(),
        "eleven minutes of waiting"
    );
    assert!(host.stopped.borrow().is_empty());
}
