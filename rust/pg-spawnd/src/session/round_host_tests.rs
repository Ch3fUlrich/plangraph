//! The host effects of the round watchdog against real processes: signals, identity, marks.

use super::{
    round_cli::run,
    round_cli_tests::{command_shell, fixture_shell, wait_for, World},
    round_host::{signal_member, LocalHost, Member},
    round_time_tests::{Transcript, MIN, T0},
    round_watch::Host,
};
use serde_json::json;
use std::{fs, path::Path};

#[test]
#[rustfmt::skip]
fn claude_stop_exit_zero_is_not_a_stopped_session_while_agents_still_lists_it_live() {
    let sid = "11111111-2222-3333-4444-555555555555";
    let host = LocalHost::new(Default::default(), sid.into());
    let live = format!(r#"[{{"sessionId":"{sid}","state":"working"}}]"#);
    let done = format!(r#"[{{"sessionId":"{sid}","state":"done"}}]"#);
    let other = r#"[{"sessionId":"other","state":"working"}]"#;
    for (stop, listing, expected) in [
        (true, Some(live.as_str()), false),
        (true, Some(done.as_str()), true),
        (true, Some(other), true),
        (true, Some(""), true),
        (true, None, false),
        (true, Some("not json"), false), (false, Some(&done), false),
    ] {
        let mut calls = Vec::new();
        let ended = host.end_session_with(|argv| {
            calls.push(argv.join(" "));
            match argv {
                ["stop", "11111111"] => stop.then(Vec::new),
                ["agents", "--json"] => listing.map(|text| text.as_bytes().to_vec()),
                _ => panic!("unexpected client argv {argv:?}"),
            }
        });
        let expected_calls = ["stop 11111111", "agents --json"];
        assert_eq!(calls, expected_calls[..if stop { 2 } else { 1 }]);
        assert_eq!(ended, expected, "{listing:?}");
    }
}

#[test]
fn claude_stop_retries_a_temporarily_busy_executable() {
    use pg_worker_core::stall::release_busy_fixture;
    let dir = tempfile::tempdir().unwrap();
    let stub = pg_worker_core::executable_test_support::write_script(
        dir.path(), "busy", "#!/bin/sh\ncase \"$*\" in 'stop 11111111') exit 0;; 'agents --json') printf '[]';; *) exit 1;; esac\n",
    );
    let mut writer = Some(fs::OpenOptions::new().write(true).open(&stub).unwrap());
    let mut host = LocalHost::new(dir.path().to_path_buf(), "11111111-session".into());
    host.claude_bin = Some(stub);

    let ended = host.end_session_with_sleeper(|pause| release_busy_fixture(&mut writer, pause));
    assert!(writer.is_none());
    assert!(ended, "a temporary executable-busy error must be retried");
}

static RACING: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

/// The member exits just before the signal arrives: the kernel refuses the signal.
fn racing_signal(member: &Member, _signal: i32) -> bool {
    RACING.store(member.pid, std::sync::atomic::Ordering::SeqCst);
    // SAFETY: kill takes numeric identifiers and dereferences nothing.
    unsafe { libc::kill(member.pid as i32, libc::SIGKILL) };
    wait_for("signalled fixture did not exit", || {
        fs::read_to_string(format!("/proc/{}/stat", member.pid))
            .ok()
            .and_then(|stat| pg_worker_core::run::group_unix::parse_stat(&stat))
            .is_none_or(|(state, _, _)| matches!(state, 'Z' | 'X'))
    });
    false
}

#[test]
fn a_member_that_exits_before_the_signal_is_no_stop_of_ours() {
    let world = World::live();
    let id = "sess-race-0001";
    let root = world.state.parent().unwrap().to_path_buf();
    let mut shell = command_shell(&root, id, "bgR", "sleep 308");
    let path = root.join("tasks/bgR.output");
    let task = super::round_wait::TaskRef {
        id: "bgR".into(),
        output_path: Some(path.display().to_string()),
        launched_from_ms: world.origin,
        launched_to_ms: world.origin + 5_000,
    };
    let mut host = world.host(id);
    host.send_signal = racing_signal;
    let owner = host.resolve_owner("writer-9", &task).expect("owner");
    assert_eq!(host.signal(&owner, &task), super::round_watch::Sent::Gone);
    assert!(shell.wait().is_ok());
    assert_ne!(
        RACING.load(std::sync::atomic::Ordering::SeqCst),
        0,
        "a signal was attempted"
    );
}

#[test]
fn a_telemetry_only_pass_books_rows_and_touches_nothing_else() {
    let world = World::new();
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u1",
        "Bash",
        json!({"command":"cargo test","run_in_background":true}),
    );
    t.result(2_500, "u1", Some("bg1"));
    t.request(3_000);
    t.tool(
        4_000,
        "w1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    world.session("writer-1", "abc", "/w/telemetry", Some(&t));
    let log = run(
        &world.state,
        &world.routing,
        &world.home,
        "ws",
        T0 + 60 * MIN,
        false,
    );
    assert!(log.is_empty(), "no stop, no notice: {log:?}");
    assert_eq!(world.rows().len(), 1);
    assert!(!world.state.join("round-watch").exists());
    assert!(!world.state.join("watchdog").exists());
}

fn member_of(pid: u32) -> Member {
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
    let (_, group, start_time) = pg_worker_core::run::group_unix::parse_stat(&text).unwrap();
    Member {
        pid,
        start_time,
        group,
    }
}

#[test]
fn a_signal_goes_only_to_the_member_that_was_seen() {
    let dir = tempfile::tempdir().unwrap();
    let mut shell = command_shell(dir.path(), "sess-id-0001", "bgI", "sleep 309");
    let seen = member_of(shell.id());
    // Another start time, or another group: a reused pid or a process that left the group.
    assert!(!signal_member(
        &Member {
            start_time: seen.start_time + 1,
            ..seen.clone()
        },
        libc::SIGTERM
    ));
    assert!(!signal_member(
        &Member {
            group: seen.group + 1,
            ..seen.clone()
        },
        libc::SIGTERM
    ));
    assert!(!signal_member(
        &Member {
            pid: 0,
            ..seen.clone()
        },
        libc::SIGTERM
    ));
    assert!(
        shell.try_wait().unwrap().is_none(),
        "none of those signals reached the process"
    );
    assert!(
        signal_member(&seen, libc::SIGTERM),
        "the member that was seen is signalled"
    );
    assert!(shell.wait().is_ok());
}

fn refusing_signal(_member: &Member, _signal: i32) -> bool {
    false
}

#[test]
fn when_no_signal_is_accepted_the_stop_is_unsignalled_and_nothing_died() {
    let world = World::live();
    let root = world.state.parent().unwrap().to_path_buf();
    let mut shell = command_shell(&root, "sess-uns-0001", "bgU", "sleep 310");
    let task = super::round_wait::TaskRef {
        id: "bgU".into(),
        output_path: Some(root.join("tasks/bgU.output").display().to_string()),
        launched_from_ms: world.origin,
        launched_to_ms: world.origin + 5_000,
    };
    let mut host = world.host("sess-uns-0001");
    host.send_signal = refusing_signal;
    let owner = host.resolve_owner("writer-9", &task).expect("owner");
    assert_eq!(
        host.signal(&owner, &task),
        super::round_watch::Sent::Refused
    );
    assert_eq!(
        host.signal(&owner, &task),
        super::round_watch::Sent::Refused
    );
    assert!(!host.group_gone(&owner, &task));
    assert!(shell.try_wait().unwrap().is_none());
    shell.kill().unwrap();
    let _ = shell.wait();
}

#[test]
fn the_owners_output_carries_one_stop_line_per_action() {
    let world = World::live();
    let root = world.state.parent().unwrap().to_path_buf();
    let mut shell = command_shell(&root, "sess-mark-0001", "bgM", "sleep 311");
    let task = super::round_wait::TaskRef {
        id: "bgM".into(),
        output_path: Some(root.join("tasks/bgM.output").display().to_string()),
        launched_from_ms: world.origin,
        launched_to_ms: world.origin + 5_000,
    };
    let host = world.host("sess-mark-0001");
    let owner = host.resolve_owner("writer-9", &task).expect("owner");
    assert!(
        owner.output.is_some(),
        "the output inode is part of the owner"
    );
    assert!(host.mark_stop(&owner, &task, "r|bgM|w1"));
    assert!(host.mark_stop(&owner, &task, "r|bgM|w2"));
    let text = fs::read_to_string(root.join("tasks/bgM.output")).unwrap();
    assert_eq!(
        text.matches("[round-watch] ending this command").count(),
        2,
        "{text}"
    );
    shell.kill().unwrap();
    let _ = shell.wait();
}

#[test]
fn the_mark_goes_to_the_owners_file_not_to_whatever_the_path_now_names() {
    let world = World::live();
    let root = world.state.parent().unwrap().to_path_buf();
    let mut shell = command_shell(&root, "sess-swap-0001", "bgW", "sleep 312");
    let path = root.join("tasks/bgW.output");
    let task = super::round_wait::TaskRef {
        id: "bgW".into(),
        output_path: Some(path.display().to_string()),
        launched_from_ms: world.origin,
        launched_to_ms: world.origin + 5_000,
    };
    let host = world.host("sess-swap-0001");
    let owner = host.resolve_owner("writer-9", &task).expect("owner");
    // After the owner was found, the path is replaced by an unrelated file.
    let moved = root.join("tasks/bgW.moved");
    fs::rename(&path, &moved).unwrap();
    fs::write(&path, "unrelated\n").unwrap();
    assert!(
        host.mark_stop(&owner, &task, "r|bgW|w1"),
        "written through the owner's descriptor"
    );
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "unrelated\n",
        "the new file is untouched"
    );
    assert!(fs::read_to_string(&moved)
        .unwrap()
        .contains("[round-watch]"));
    // A forged owner whose recorded inode is another file's is refused.
    let mut forged = owner.clone();
    forged.output = Some((1, 1));
    assert!(!host.mark_stop(&forged, &task, "r|bgW|w2"));
    shell.kill().unwrap();
    let _ = shell.wait();
}

#[test]
fn a_command_that_ignores_terminate_is_killed_by_the_verified_escalation() {
    let world = World::live();
    let root = world.state.parent().unwrap().to_path_buf();
    let tasks = root.join("tasks");
    let script = "trap '' TERM; : > trap-ready; exec /bin/sleep 313";
    let mut shell = fixture_shell(&root, "sess-hup-0001", "bgH", script, false);
    let task = super::round_wait::TaskRef {
        id: "bgH".into(),
        output_path: Some(tasks.join("bgH.output").display().to_string()),
        launched_from_ms: world.origin,
        launched_to_ms: world.origin + 5_000,
    };
    let host = world.host("sess-hup-0001");
    let owner = host.resolve_owner("writer-9", &task).expect("owner");
    assert_eq!(
        host.signal(&owner, &task),
        super::round_watch::Sent::Accepted
    );
    assert_eq!(
        host.finish_stop(&owner, &task),
        super::round_watch::Stop::Ended
    );
    assert!(host.group_gone(&owner, &task));
    let status = shell.wait().unwrap();
    assert!(!status.success(), "ended by a signal, not by itself");
}

#[test]
fn a_command_that_redirects_its_output_when_signalled_is_still_the_owner_and_still_running() {
    let world = World::live();
    let root = world.state.parent().unwrap().to_path_buf();
    let tasks = root.join("tasks");
    // On SIGTERM the shell moves its own stdout away and carries on.
    let mut shell = fixture_shell(
        &root, "sess-redir-0001", "bgR",
        "trap 'exec 1>/dev/null 2>&1; : > redirected' TERM; : > trap-ready; while :; do read -r line; done",
        false,
    );
    let task = super::round_wait::TaskRef {
        id: "bgR".into(),
        output_path: Some(tasks.join("bgR.output").display().to_string()),
        launched_from_ms: world.origin,
        launched_to_ms: world.origin + 5_000,
    };
    let host = world.host("sess-redir-0001");
    let owner = host.resolve_owner("writer-9", &task).expect("owner");
    assert_eq!(
        host.signal(&owner, &task),
        super::round_watch::Sent::Accepted
    );
    let redirected = root.join("redirected");
    wait_for("redirect marker missing", || redirected.is_file());
    assert!(
        shell.try_wait().unwrap().is_none(),
        "the shell survived the signal"
    );
    assert!(
        !host.group_gone(&owner, &task),
        "the same leader runs on, whatever it writes to now"
    );
    assert_eq!(
        host.finish_stop(&owner, &task),
        super::round_watch::Stop::Ended
    );
    assert!(
        shell.try_wait().unwrap().is_some() || shell.wait().is_ok(),
        "ended by the escalation"
    );
    assert!(host.group_gone(&owner, &task));
}
