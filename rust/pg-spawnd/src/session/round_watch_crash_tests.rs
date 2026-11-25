//! Crash, receipt and delivery-outage cases of the round watchdog.

use super::round_time_tests::{Transcript, MIN, T0};
use super::round_watch::{blocked_outcome, Action, Stop};
use super::round_watch_tests::{first_wait, run, waiting, Fake};
use serde_json::json;

const KEY: &str = "writer-1:p1|a|w-a";

/// The watchdog marked the owner's output, killed the command and died before it saved `Stopped`.
fn crashed_after_the_kill(dir: &std::path::Path, t: &Transcript) {
    let path = dir.join("w.json");
    let mut state: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    state["actions"]["writer-1:p1|a|w-a"] = json!({"phase": "stopping", "marked": true, "marked_ms": T0 + 15 * MIN,
        "round_key": "writer-1:p1", "task_id": "a", "call_id": "w-a",
        "command": "cargo test -p a", "waited_ms": 1,
        "task": {"id": "a", "output_path": "/t/tasks/a.output",
                 "launched_from_ms": 0, "launched_to_ms": 1}});
    std::fs::write(&path, state.to_string()).unwrap();
    let _ = t;
}

fn ended_by(t: &mut Transcript, code: i64) {
    t.result(16 * MIN, "w-a", None);
    t.0.push(json!({"type":"user","timestamp": super::round_time_tests::stamp_from(T0, 16 * MIN),
        "origin":{"kind":"task-notification"},
        "message":{"role":"user","content": format!(
            "<task-notification><task-id>a</task-id><status>failed</status><summary>Background command \"cargo test -p a\" failed with exit code {code}</summary></task-notification>")}}));
}

#[test]
fn a_crash_after_the_kill_is_recovered_from_the_harness_report_of_the_signal_exit() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    crashed_after_the_kill(dir.path(), &t);
    ended_by(&mut t, 129);
    let restarted = Fake::live();
    restarted.marks.borrow_mut().insert(KEY.into());
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
    assert!(
        restarted.stopped.borrow().is_empty(),
        "no new signal without an open wait"
    );
    assert_eq!(restarted.sent.borrow().len(), 1, "the nudge is not lost");
}

#[test]
fn a_crash_before_the_kill_and_a_normal_exit_is_released_without_a_strike() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    crashed_after_the_kill(dir.path(), &t);
    ended_by(&mut t, 0);
    let restarted = Fake::live();
    restarted.marks.borrow_mut().insert(KEY.into());
    assert!(run(&t, 17, dir.path(), &restarted).is_empty());
    assert!(restarted.sent.borrow().is_empty());
    assert!(std::fs::read_to_string(dir.path().join("w.json"))
        .unwrap()
        .contains("released"));
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
fn an_independent_signal_exit_without_the_mark_is_no_stop_of_ours() {
    // The watchdog died before it marked the output, so before any signal; the command
    // then ended by itself with 143: the exit code alone proves nothing.
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    crashed_after_the_kill(dir.path(), &t);
    ended_by(&mut t, 143);
    let restarted = Fake::live(); // no mark in the owner's output
    assert!(run(&t, 17, dir.path(), &restarted).is_empty());
    assert!(restarted.sent.borrow().is_empty());
    assert!(restarted.stopped.borrow().is_empty());
}

#[test]
fn a_marked_stop_stays_pending_without_a_new_signal_until_the_harness_reports() {
    let dir = tempfile::tempdir().unwrap();
    let host = Fake::live();
    let mut t = first_wait("a");
    run(&t, 1, dir.path(), &host);
    crashed_after_the_kill(dir.path(), &t);
    // The wait returned, the group is gone, the exit report has not arrived yet.
    t.result(16 * MIN, "w-a", None);
    let restarted = Fake::live();
    restarted.marks.borrow_mut().insert(KEY.into());
    assert!(run(&t, 17, dir.path(), &restarted).is_empty());
    assert!(
        restarted.stopped.borrow().is_empty(),
        "no signal after the wait closed"
    );
    assert!(!std::fs::read_to_string(dir.path().join("w.json"))
        .unwrap()
        .contains("released"));
    // The report arrives: a signal exit settles it as a stop with its nudge.
    t.0.push(json!({"type":"user","timestamp": super::round_time_tests::stamp_from(T0, 17 * MIN),
        "origin":{"kind":"task-notification"},
        "message":{"role":"user","content":
            "<task-notification><task-id>a</task-id><status>failed</status><summary>Background command \"x\" failed with exit code 129</summary></task-notification>"}}));
    let done = run(&t, 18, dir.path(), &restarted);
    assert!(
        done.contains(&Action::Notified {
            task_id: "a".into()
        }),
        "{done:?}"
    );
    // A report that never comes releases the stop after the grace.
    let dir2 = tempfile::tempdir().unwrap();
    let mut silent = first_wait("a");
    run(&silent, 1, dir2.path(), &host);
    crashed_after_the_kill(dir2.path(), &silent);
    silent.result(16 * MIN, "w-a", None);
    let late = Fake::live();
    late.marks.borrow_mut().insert(KEY.into());
    assert!(run(&silent, 17, dir2.path(), &late).is_empty());
    assert!(run(&silent, 16 + 11, dir2.path(), &late).is_empty());
    assert!(std::fs::read_to_string(dir2.path().join("w.json"))
        .unwrap()
        .contains("released"));
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
fn a_stop_that_reached_no_member_records_no_signal_and_a_later_natural_exit_is_no_strike() {
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
        .contains("\"signalled\":true"));
    // The command now ends by itself: the host finds nothing and no signal is on record.
    let later = Fake::new(true, Stop::Gone);
    assert!(
        run(&t, 17, dir.path(), &later).is_empty(),
        "gone, marked, no report yet"
    );
    assert_eq!(
        run(&t, 28, dir.path(), &later),
        vec![Action::Finished {
            task_id: "a".into()
        }]
    );
    assert!(later.sent.borrow().is_empty());
    assert!(
        later.seen_signalled.borrow().iter().all(|s| !s),
        "no signal was ever on record"
    );
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
    assert_eq!(retry.stopped.borrow().len(), 1);
}

#[test]
fn an_exit_report_of_our_signal_is_129_and_an_independent_143_proves_nothing() {
    for (code, ours) in [(129, true), (143, false), (137, false), (0, false)] {
        let dir = tempfile::tempdir().unwrap();
        let host = Fake::live();
        let mut t = first_wait("a");
        run(&t, 1, dir.path(), &host);
        crashed_after_the_kill(dir.path(), &t);
        ended_by(&mut t, code);
        let restarted = Fake::live();
        restarted.marks.borrow_mut().insert(KEY.into());
        let done = run(&t, 17, dir.path(), &restarted);
        let stopped = done.iter().any(|a| matches!(a, Action::Stopped { .. }));
        assert_eq!(stopped, ours, "exit {code}: {done:?}");
        assert!(
            restarted.stopped.borrow().is_empty(),
            "no signal without an open call"
        );
    }
}
