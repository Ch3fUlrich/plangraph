use super::*;

#[test]
fn supervise_events_samples_tree_and_cpu_into_monitor() {
    use crate::{activity::*, events::ChannelHealth, hosts::Role, watch::*};
    use std::{cell::Cell, time::Duration};
    let (trees, cpus) = (Cell::new(0), Cell::new(0));
    let mut parser = crate::watch::blind_tests::Parser::default();
    let mut channel = ChannelHealth::new(0, Some(1));
    let mut monitor = Monitor::new(
        Role::Research,
        &Limits {
            first_activity_s: Some(1),
            milestone_s: Some(1),
            ..Limits::default()
        },
        Modes {
            first: Mode::On,
            silent: Mode::On,
            milestone: Mode::On,
        },
        0,
    );
    let mut cpu = |ts_ms| {
        cpus.set(cpus.get() + 1);
        Some(Signal {
            kind: Kind::Content,
            ts_ms,
            tool: None,
        })
    };
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "exec sleep 1.2"]);
    let r = supervise_events(
        cmd,
        &crate::stall::Opts {
            timeout: Some(Duration::from_secs(3)),
            ..crate::stall::Opts::default()
        },
        Hooks {
            parser: &mut parser,
            monitor: &mut monitor,
            channel: &mut channel,
            cancelled: &|| false,
            tree_sample: &|| {
                trees.set(trees.get() + 1);
                Ctx {
                    edited: true,
                    safe_alt: true,
                }
            },
            cpu: Some(&mut cpu),
            stdin: b"",
        },
    )
    .unwrap();
    assert_eq!(trees.get(), 1);
    assert!(cpus.get() > 0);
    assert!(r.activity.is_some());
    assert!(monitor.last_strong_ms().is_some());
    assert!(r.decisions.is_empty()); // edited tree satisfies milestone; CPU satisfies first activity.
    assert_eq!(r.run.code, Some(0));
}

#[test]
fn foundation_docs_exclude_followup_slice_narrative() {
    let docs = include_str!("../../../docs/spawner-reliability.md");
    let (_, foundation) = docs
        .split_once("The RL12 B1 foundation")
        .expect("missing RL12 B1 foundation documentation");
    let (foundation, _) = foundation
        .split_once("The remaining behavior")
        .expect("missing RL12 B1 follow-up boundary");
    assert!(!foundation.contains("Stop retains"));
    assert!(!foundation.contains("stdin writer is retained"));
    assert!(foundation.contains("Watched.io_error"));
    assert!(foundation.contains("`killpg` rejects"));
}

#[test]
fn stdin_writer_cancels_when_child_does_not_read() {
    let mut cmd = Command::new("sleep");
    cmd.arg("30").stdin(std::process::Stdio::piped());
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    let zeros = vec![0u8; 1024 * 1024];
    let writer = stdin_writer(child.child.stdin.take().unwrap(), &zeros);
    let started = std::time::Instant::now();
    let cleanup = child.kill();
    let result = writer.unwrap().finish();
    child.child.wait().unwrap();
    result.unwrap();
    cleanup.unwrap();
    assert!(started.elapsed() < std::time::Duration::from_secs(5));
}

#[test]
fn stdin_teardown_kills_before_joining_writer() {
    let source = include_str!("watch_process_tests.rs");
    let (_, test) = source
        .split_once("fn stdin_writer_cancels_when_child_does_not_read()")
        .unwrap();
    let (test, _) = test.split_once("\n#[test]").unwrap();
    assert!(test.find("child.kill()").unwrap() < test.find(".finish()").unwrap());
    assert!(test.contains("let zeros = vec![0u8; 1024 * 1024];"));
    assert!(test.contains("&zeros)"));
}

#[test]
fn stdin_writer_is_nonblocking_and_stop_aware() {
    let mut cmd = Command::new("sleep");
    cmd.arg("30").stdin(std::process::Stdio::piped());
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    let zeros = vec![0u8; 1024 * 1024];
    let writer = stdin_writer(child.child.stdin.take().unwrap(), &zeros).unwrap();
    // Let the unread pipe fill before cancellation, rather than racing startup.
    std::thread::sleep(std::time::Duration::from_millis(100));
    let started = std::time::Instant::now();
    let result = writer.finish();
    let alive = child.try_wait().unwrap().is_none();
    child.kill().unwrap();
    child.child.wait().unwrap();
    result.unwrap();
    assert!(alive);
    assert!(started.elapsed() < std::time::Duration::from_secs(5));
}

#[test]
fn grandchild_group_killed_even_after_leader_exit() {
    let mut sentinel_cmd = Command::new("sleep");
    sentinel_cmd.arg("30");
    let mut sentinel = UnixChild::spawn(&mut sentinel_cmd).unwrap();
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "sleep 30 & exit 0"]);
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    // Always clean up our known group, including when an assertion unwinds.
    struct Cleanup(i32);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = killpg(self.0, libc::SIGKILL);
        }
    }
    let _cleanup = Cleanup(child.pgid);
    let _sentinel_cleanup = Cleanup(sentinel.pgid);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while child.try_wait().unwrap().is_none() {
        assert!(std::time::Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert!(!child.empty().unwrap());
    child.kill().unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    // A zombie may remain until the host's init reaps it: verify no live member.
    loop {
        let live = std::fs::read_dir("/proc").unwrap().flatten().any(|entry| {
            let Ok(stat) = std::fs::read_to_string(entry.path().join("stat")) else {
                return false;
            };
            let Some((_, fields)) = stat.rsplit_once(") ") else {
                return false;
            };
            let fields: Vec<_> = fields.split_whitespace().collect();
            fields.get(2) == Some(&child.pgid.to_string().as_str()) && fields[0] != "Z"
        });
        if !live {
            break;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert!(sentinel.try_wait().unwrap().is_none());
    sentinel.kill().unwrap();
    sentinel.child.wait().unwrap();
}

#[test]
fn watch_esrch_latches_and_never_signals_again() {
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "exit 0"]);
    let mut child = UnixChild::spawn(&mut cmd).unwrap();
    child.child.wait().unwrap();
    assert!(killpg(child.pgid, libc::SIGTERM).is_ok());
    assert!(killpg(child.pgid, libc::SIGKILL).is_ok());
    assert!(child.empty().unwrap());
    assert!(child.gone);
    child.pgid = unsafe { libc::getpgrp() };
    assert!(child.empty().unwrap());
    child.kill().unwrap();
}
