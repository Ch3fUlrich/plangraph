use super::super::blind_tests::SCHEDULER_SLACK;
use super::*;
use crate::{activity::*, hosts::Role};

#[test]
fn honored_term_finishes_before_long_grace() {
    let mut parser = super::super::blind_tests::Parser::default();
    let (mut monitor, mut channel) = (monitor(), ChannelHealth::new(0, Some(1)));
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "trap 'exit 0' TERM; while :; do :; done"]);
    let start = Instant::now();
    let r = supervise_events(
        cmd,
        &stall::Opts {
            timeout: Some(Duration::from_secs(10)),
            grace: Duration::from_secs(5),
            ..stall::Opts::default()
        },
        Hooks {
            parser: &mut parser,
            monitor: &mut monitor,
            channel: &mut channel,
            cancelled: &|| false,
            tree_sample: &Ctx::default,
            cpu: None,
            stdin: b"",
        },
    )
    .unwrap();
    assert_eq!(r.proof, KillProof::Empty);
    assert!(start.elapsed() < Duration::from_secs(2) + SCHEDULER_SLACK);
}

#[test]
fn retained_error_survives_nonempty_and_later_errors() {
    let mut error = Some(io::ErrorKind::BrokenPipe.into());
    retain_error(&mut error, Ok(false));
    retain_error(&mut error, Err(io::ErrorKind::PermissionDenied.into()));
    assert_eq!(error.unwrap().kind(), io::ErrorKind::BrokenPipe);
}

#[test]
fn flooding_stderr_obeys_one_second_timeout() {
    let start = Instant::now();
    let r = super::super::blind_tests::run_timeout(
        "exec yes flood >&2",
        true,
        b"",
        false,
        Duration::from_secs(1),
    );
    assert!(r.run.timed_out);
    assert!(start.elapsed() < Duration::from_millis(3100) + SCHEDULER_SLACK);
}

#[test]
fn queued_signal_and_bad_ratio_survive_drain_boundaries() {
    for bad in [false, true] {
        let mut parser = super::super::blind_tests::Parser::default();
        let (mut monitor, mut channel) = (monitor(), ChannelHealth::new(0, Some(1)));
        assert_eq!(monitor.tick(800, Ctx::default()).unwrap().act, Act::Warn);
        let mut hooks = Hooks {
            parser: &mut parser,
            monitor: &mut monitor,
            channel: &mut channel,
            cancelled: &|| false,
            tree_sample: &Ctx::default,
            cpu: None,
            stdin: b"",
        };
        let mut events = Events {
            last_byte: 0,
            output: vec![],
            eof: 0,
            provider: false,
            late: None,
            blind: None,
            partial: false,
        };
        let (tx, rx) = mpsc::sync_channel(300);
        for _ in 0..300 {
            tx.send(Chunk::Data(false, vec![b'x'])).unwrap();
        }
        events.drain(&rx, &mut hooks, 0, false, 1024).unwrap();
        assert_eq!(events.output.len(), 256);
        events.drain(&rx, &mut hooks, 0, false, 1024).unwrap();
        assert_eq!(events.output.len(), 300);
        if bad {
            tx.send(Chunk::Data(true, b"bad\n".repeat(20))).unwrap();
            tx.send(Chunk::Error(io::ErrorKind::BrokenPipe.into()))
                .unwrap();
        } else {
            for _ in 0..69 {
                tx.send(Chunk::Data(true, b"{}\n".repeat(2730))).unwrap();
            }
            tx.send(Chunk::Data(true, b"{\"content\":true}\n".to_vec()))
                .unwrap();
        }
        let result = events.drain(&rx, &mut hooks, if bad { 0 } else { 1999 }, false, 1024);
        if bad {
            assert!(result.is_err());
            assert_eq!(events.blind, Some(BlindReason::BadRatio));
        } else {
            result.unwrap();
            assert_eq!(hooks.monitor.last_strong_ms(), Some(1999));
            assert!(hooks.monitor.tick(2000, Ctx::default()).is_none());
        }
    }
}

#[test]
fn writer_error_preserves_watched() {
    let watched = super::super::blind_tests::run("exit 0", false, &vec![0; 1 << 20], false);
    let watched = record_writer_result(watched, Err(io::ErrorKind::WriteZero.into()));
    assert_eq!(watched.io_error.unwrap().kind(), io::ErrorKind::WriteZero);
    assert_eq!(watched.run.code, Some(0));
    assert_eq!(watched.proof, KillProof::NotNeeded);
    assert_eq!(watched.channel.lines, 0);
    assert!(watched.decisions.is_empty());
}

#[test]
fn mixed_kind_mirror_counts_both_lines() {
    let r =
        super::super::blind_tests::run("printf '{}\\n{\"unknown\":true}\\n'", false, b"", false);
    assert_eq!((r.channel.lines, r.channel.bad), (2, 0));
}

#[test]
fn natural_exit_does_not_sample_cancel_during_drain() {
    super::super::unix::PROBES.with(|n| n.set(0));
    let mut parser = super::super::blind_tests::Parser::default();
    let mut monitor = monitor();
    let mut channel = ChannelHealth::new(0, Some(1));
    let start = Instant::now();
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "setsid sh -c 'sleep 3' & exit 0"]);
    let r = supervise_events(
        cmd,
        &stall::Opts {
            timeout: Some(Duration::from_secs(5)),
            ..stall::Opts::default()
        },
        Hooks {
            parser: &mut parser,
            monitor: &mut monitor,
            channel: &mut channel,
            cancelled: &|| start.elapsed() >= Duration::from_millis(500),
            tree_sample: &Ctx::default,
            cpu: None,
            stdin: b"",
        },
    )
    .unwrap();
    assert_eq!(r.run.code, Some(0));
    assert!(!r.run.cancelled);
    assert_eq!(super::super::unix::PROBES.with(|n| n.get()), 2);
}

#[test]
fn writer_setup_failure_stops_group_without_monitor_tick() {
    let mut parser = super::super::blind_tests::Parser::default();
    let (mut monitor, mut channel) = (monitor(), ChannelHealth::new(0, Some(1)));
    let mut cmd = Command::new("sh");
    cmd.args(["-c", "exec sleep 20"]);
    let start = Instant::now();
    let r = supervise_with_writer(
        cmd,
        &stall::Opts {
            timeout: Some(Duration::from_secs(5)),
            grace: Duration::from_millis(50),
            ..stall::Opts::default()
        },
        Hooks {
            parser: &mut parser,
            monitor: &mut monitor,
            channel: &mut channel,
            cancelled: &|| false,
            tree_sample: &Ctx::default,
            cpu: None,
            stdin: b"",
        },
        |_, _| {
            thread::sleep(Duration::from_millis(900));
            Err(io::ErrorKind::PermissionDenied.into())
        },
    )
    .unwrap();
    assert_eq!(r.io_error.unwrap().kind(), io::ErrorKind::PermissionDenied);
    assert_eq!(r.proof, KillProof::Empty);
    assert!(r.decisions.is_empty());
    assert!(start.elapsed() < Duration::from_millis(3500));
}

#[test]
fn reap_in_stop_retains_exit_status() {
    struct Exited;
    impl Child for Exited {
        fn try_wait(&mut self) -> io::Result<Option<std::process::ExitStatus>> {
            Ok(None)
        }
        fn reap(&mut self) -> io::Result<Option<std::process::ExitStatus>> {
            Ok(Some(std::process::ExitStatus::from_raw(0)))
        }
        fn empty(&mut self) -> io::Result<bool> {
            Ok(true)
        }
        fn terminate(&mut self) -> io::Result<()> {
            unreachable!()
        }
        fn kill(&mut self) -> io::Result<()> {
            unreachable!()
        }
    }
    let mut stop = Stop::default();
    assert_eq!(stop.tick(&mut Exited, 0, 100, 2000), Some(KillProof::Empty));
    assert_eq!(stop.status.unwrap().code(), Some(0));
}

#[test]
fn missing_wall_timeout_rejected_before_spawn() {
    let mut parser = super::super::blind_tests::Parser::default();
    let mut monitor = monitor();
    let mut channel = ChannelHealth::new(0, Some(1));
    let result = supervise_events(
        Command::new("nonexistent-watch-command"),
        &stall::Opts::default(),
        Hooks {
            parser: &mut parser,
            monitor: &mut monitor,
            channel: &mut channel,
            cancelled: &|| false,
            tree_sample: &Ctx::default,
            cpu: None,
            stdin: b"",
        },
    );
    assert_eq!(result.err().unwrap().kind(), io::ErrorKind::InvalidInput);
}

#[test]
fn blocked_stdin_writer_releases_prompt_before_pipe_closes() {
    let mut child = Command::new("sh")
        .args(["-c", "exec sleep 5"])
        .stdin(Stdio::piped())
        .spawn()
        .unwrap();
    let writer =
        super::super::unix::stdin_writer(child.stdin.take().unwrap(), &vec![0; 1 << 20]).unwrap();
    let start = Instant::now();
    let result = writer.finish();
    let elapsed = start.elapsed();
    child.kill().unwrap();
    child.wait().unwrap();
    result.unwrap();
    assert!(elapsed < Duration::from_secs(2));
}

fn monitor() -> Monitor {
    Monitor::new(
        Role::Research,
        &Limits {
            first_activity_s: Some(1),
            silence_s: Some(1),
            confirm_s: Some(1),
            milestone_s: Some(1),
            ..Limits::default()
        },
        Modes {
            first: Mode::On,
            silent: Mode::On,
            milestone: Mode::On,
        },
        0,
    )
}
fn signal(kind: Kind, ts_ms: u64) -> Signal {
    Signal {
        kind,
        ts_ms,
        tool: None,
    }
}

#[test]
fn blind_latched_before_error_aborts_drain() {
    use super::super::blind_tests::Parser;
    let mut parser = Parser::default();
    let (mut monitor, mut channel) = (monitor(), ChannelHealth::new(0, Some(1)));
    channel.pin("fixture", "drift");
    let mut hooks = Hooks {
        parser: &mut parser,
        monitor: &mut monitor,
        channel: &mut channel,
        cancelled: &|| false,
        tree_sample: &Ctx::default,
        cpu: None,
        stdin: b"",
    };
    let mut events = Events {
        last_byte: 0,
        output: vec![],
        eof: 0,
        provider: false,
        late: None,
        blind: None,
        partial: false,
    };
    let (tx, rx) = mpsc::sync_channel(2);
    tx.send(Chunk::Data(true, b"{\"error\":true}\n".to_vec()))
        .unwrap();
    tx.send(Chunk::Error(io::ErrorKind::BrokenPipe.into()))
        .unwrap();
    assert!(events.drain(&rx, &mut hooks, 0, false, 1024).is_err());
    assert!(!events.provider);
}
#[test]
fn signal_in_confirm_cancels_kill_and_late_signal_does_not() {
    let mut m = monitor();
    assert_eq!(m.tick(800, Ctx::default()).unwrap().act, Act::Warn);
    m.feed(signal(Kind::Content, 1999));
    assert!(m.tick(2000, Ctx::default()).is_none());
    assert_eq!(m.tick(2800, Ctx::default()).unwrap().act, Act::Warn);
    assert_eq!(m.tick(3999, Ctx::default()).unwrap().act, Act::Kill);
    m.feed(signal(Kind::Content, 4000));
    assert_eq!(m.state(), State::Ended(Rule::Silent));
}

#[test]
fn tree_sample_and_cpu_hooks_do_not_bypass_monitor() {
    let mut m = monitor();
    m.feed(signal(Kind::Weak, 900));
    assert_eq!(m.state(), State::Pending);
    m.tick(
        900,
        Ctx {
            edited: true,
            safe_alt: true,
        },
    );
    assert_eq!(
        m.tick(2000, Ctx::default()).unwrap().rule,
        Rule::FirstActivity
    );
    let mut m = monitor();
    m.feed(signal(Kind::Content, 0));
    m.feed(signal(Kind::Weak, 1500));
    assert_eq!(m.last_strong_ms(), Some(0));
    assert_eq!(
        m.tick(
            1500,
            Ctx {
                edited: false,
                safe_alt: true
            }
        )
        .unwrap()
        .rule,
        Rule::Milestone
    );
}

#[test]
fn blind_transition_is_sticky_and_once() {
    let mut h = ChannelHealth::new(0, Some(1));
    for _ in 0..20 {
        h.line(crate::events::LineKind::NotJson, 0);
    }
    let first = h.blind(0);
    for _ in 0..100 {
        h.line(crate::events::LineKind::Known, 2000);
    }
    assert_eq!(first, Some(BlindReason::BadRatio));
    assert_eq!(h.blind(3000), first);
}

#[test]
fn parsing_channel_never_arms_byte_rule() {
    let mut h = ChannelHealth::new(0, Some(1));
    h.line(crate::events::LineKind::Known, 0);
    assert_eq!(h.blind(1_000_000), None);
}

#[test]
fn cancel_timeout_provider_converge() {
    let cancel = super::super::blind_tests::run("exec sleep 20", false, b"", true);
    assert!(cancel.run.cancelled);
    assert_eq!(cancel.proof, KillProof::Empty);
    let provider = super::super::blind_tests::run(
        "printf '{\"error\":true}\\n'; exec sleep 20",
        false,
        b"",
        false,
    );
    assert_eq!(provider.proof, KillProof::Empty);
    assert!(provider.decisions.is_empty());
}
