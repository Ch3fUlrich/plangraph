use super::*;
use crate::{
    activity::*,
    events::{Framer, Line, LineKind},
    hosts::Role,
};
use std::{
    process::Command,
    time::{Duration, Instant},
};

const FIXTURE_FIRST_S: u64 = 2;
const FIXTURE_CONFIRM_S: u64 = 2;
const FIXTURE_SILENCE_S: u64 = 3;
const ESCAPED_FIXTURE_TIMEOUT: Duration = Duration::from_secs(3);

#[test]
fn fixture_scheduler_slack_is_separate_from_contract() {
    let margin = |limit, emission| limit - emission;
    assert!(margin(FIXTURE_FIRST_S + FIXTURE_CONFIRM_S, 2) >= 2);
    assert!(margin(FIXTURE_SILENCE_S + FIXTURE_CONFIRM_S, 1) >= 2);
    assert!(ESCAPED_FIXTURE_TIMEOUT >= Duration::from_secs(3));
}

pub(super) struct Parser {
    framer: Framer,
    health: ChannelHealth,
}
impl Default for Parser {
    fn default() -> Self {
        Self {
            framer: Framer::new(),
            health: ChannelHealth::new(0, Some(1)),
        }
    }
}
impl EventParser for Parser {
    fn health(&self) -> &ChannelHealth {
        &self.health
    }
    fn feed(&mut self, chunk: &[u8], now: u64) -> Vec<Signal> {
        if chunk == b"\n" {
            self.health.line(LineKind::NotJson, now);
        }
        self.framer
            .push(chunk)
            .into_iter()
            .filter_map(|line| {
                let kind = match &line {
                    Line::Bytes(b) if b == b"{}" => Some(Kind::Request),
                    Line::Bytes(b) if b == b"{\"content\":true}" => Some(Kind::Content),
                    Line::Bytes(b) if b == b"{\"tool\":true}" => Some(Kind::Tool),
                    Line::Bytes(b) if b == b"{\"error\":true}" => Some(Kind::Error),
                    _ => None,
                };
                self.health.line(
                    if kind.is_some() {
                        LineKind::Known
                    } else if matches!(line, Line::Bytes(b) if b == b"{\"unknown\":true}") {
                        LineKind::Unknown
                    } else {
                        LineKind::NotJson
                    },
                    now,
                );
                kind.map(|kind| Signal {
                    kind,
                    ts_ms: now,
                    tool: (kind == Kind::Tool).then_some((ToolClass::Read, Phase::Start)),
                })
            })
            .collect()
    }
}
pub(super) fn run(script: &str, blind: bool, input: &[u8], cancel: bool) -> Watched {
    run_timeout(script, blind, input, cancel, Duration::from_secs(8))
}
fn run_timeout(
    script: &str,
    blind: bool,
    input: &[u8],
    cancel: bool,
    timeout: Duration,
) -> Watched {
    let mut cmd = Command::new("sh");
    cmd.args(["-c", script]);
    let mut parser = Parser {
        framer: Framer::new(),
        health: ChannelHealth::new(0, Some(1)),
    };
    let mut channel = ChannelHealth::new(0, Some(1));
    if blind {
        channel.pin("fixture", "drift");
        parser.health.pin("fixture", "drift");
    }
    let limits = Limits {
        first_activity_s: Some(FIXTURE_FIRST_S),
        silence_s: Some(FIXTURE_SILENCE_S),
        confirm_s: Some(FIXTURE_CONFIRM_S),
        milestone_s: None,
        ..Limits::default()
    };
    let mut monitor = Monitor::new(
        Role::Research,
        &limits,
        Modes {
            first: Mode::On,
            silent: Mode::On,
            milestone: Mode::On,
        },
        0,
    );
    let opts = stall::Opts {
        stall: Some(Duration::from_millis(2500)),
        timeout: Some(timeout),
        grace: Duration::from_millis(100),
        max_output: 1024,
    };
    supervise_events(
        cmd,
        &opts,
        Hooks {
            parser: &mut parser,
            monitor: &mut monitor,
            cancelled: &|| cancel,
            tree_sample: &Ctx::default,
            cpu: None,
            stdin: input,
            channel: &mut channel,
        },
    )
    .unwrap()
}
#[test]
fn pinned_error_in_first_chunk_is_not_enforced() {
    let r = run("printf '{\"error\":true}\\n'; sleep 1", true, b"", false);
    assert_eq!(r.run.code, Some(0));
    assert_eq!(r.channel.blind, Some(BlindReason::VersionDrift));
}
#[test]
fn garbage_lines_blind_no_f_s_m_kill() {
    let r = run(
        "i=0; while [ $i -lt 25 ]; do echo garbage; i=$((i+1)); done; sleep 3",
        false,
        b"",
        false,
    );
    assert_eq!(r.channel.blind, Some(BlindReason::BadRatio));
    assert_eq!(r.channel.blind_events, 1);
    assert!(r
        .decisions
        .iter()
        .all(|d| matches!(d.act, Act::WouldWarn | Act::WouldKill)));
}
#[test]
fn parsing_long_silent_tool_survives_byte_deadline() {
    let r = run("printf '{\"tool\":true}\\n'; sleep 3", false, b"", false);
    assert_eq!(r.run.code, Some(0));
    assert!(!r.stall_bytes);
    assert_eq!(r.channel.blind, None);
}
#[test]
fn no_first_activity_ignorer_gone_within_f_confirm_grace_two_second_proof() {
    sigterm_ignorer_reaped();
}
#[test]
fn escaped_pipe_holder_cannot_hang_teardown() {
    let start = Instant::now();
    // Escaped fixture expires itself; it is never targeted by the owned group.
    let r = run_timeout(
        "setsid sh -c 'sleep 4' & exit 0",
        false,
        b"",
        false,
        ESCAPED_FIXTURE_TIMEOUT,
    );
    assert_eq!(r.run.code, Some(0));
    assert!(!r.run.timed_out && !r.run.stalled);
    assert!(start.elapsed() < Duration::from_millis(3500));
}

#[test]
fn nineteen_good_lines_eof_has_no_bad_frame() {
    let r = run(
        "i=0; while [ $i -lt 19 ]; do echo '{}'; i=$((i+1)); done",
        false,
        b"",
        false,
    );
    assert_eq!((r.channel.lines, r.channel.bad), (19, 0));
}
#[test]
fn late_signal_during_term_grace_is_recorded() {
    let r = run(
        "trap 'printf \"{\\\"content\\\":true}\\n\"; exit 0' TERM; while :; do :; done",
        false,
        b"",
        false,
    );
    assert!(r.late_signal_ms.is_some());
    assert_eq!(r.run.code, Some(0));
    assert_eq!(r.decisions.last().unwrap().act, Act::Kill);
}
#[test]
fn confirm_window_signal_prevents_first_activity_kill() {
    let r = run(
        "sleep 2; printf '{\"content\":true}\\n'; sleep 1",
        false,
        b"",
        false,
    );
    assert_eq!(r.run.code, Some(0));
    assert!(r.decisions.iter().all(|d| d.act != Act::Kill));
}
#[test]
fn silent_at_f() {
    let r = run("exec sleep 20", false, b"", false);
    assert_eq!(
        r.decisions.last().unwrap().outcome,
        KillOutcome::HangNoActivity
    );
    assert_eq!(r.proof, KillProof::Empty);
}
#[test]
fn request_then_silence_at_s() {
    let r = run("printf '{}\\n'; exec sleep 20", false, b"", false);
    assert_eq!(r.decisions.last().unwrap().outcome, KillOutcome::HangSilent);
}
#[test]
fn slow_but_alive_survives() {
    let r = run(
        "printf '{\"content\":true}\\n'; sleep 1; printf '{\"content\":true}\\n'",
        false,
        b"",
        false,
    );
    assert_eq!(r.run.code, Some(0));
}
#[test]
fn sigterm_ignorer_reaped() {
    let start = Instant::now();
    let r = run("trap '' TERM; while :; do :; done", false, b"", false);
    assert_eq!(r.run.signal, Some(libc::SIGKILL));
    assert_eq!(r.proof, KillProof::Empty);
    assert!(start.elapsed() < Duration::from_millis(2_000 + 2_000 + 100 + 2_000 + 10 + 1500));
}
#[test]
fn stdin_one_megabyte_delivered_then_eof() {
    let r = run("wc -c", false, &vec![b'x'; 1 << 20], false);
    assert_eq!(String::from_utf8(r.run.output).unwrap().trim(), "1048576");
}
#[test]
fn stdout_stderr_drained_separately() {
    let r = run("printf '{}\\n' >&2; printf 'out\\n'", false, b"", false);
    assert_eq!(r.activity, None);
    assert!(r.run.output.windows(3).any(|w| w == b"out"));
}
#[test]
fn version_drift_long_silence_stall_bytes_hang_silent_one_channel_blind() {
    let r = run("exec sleep 20", true, b"", false);
    assert!(r.stall_bytes && r.run.stalled);
    assert_eq!(r.channel.blind_events, 1);
    assert!(r
        .decisions
        .iter()
        .all(|d| matches!(d.act, Act::WouldWarn | Act::WouldKill)));
}
#[test]
fn blind_continuous_bytes_not_killed_by_stall() {
    let r = run(
        "for n in 1 2 3; do printf x; sleep 1; done",
        true,
        b"",
        false,
    );
    assert!(!r.stall_bytes);
    assert_eq!(r.run.code, Some(0));
}
#[test]
fn max_timeout_no_warn_and_cancel_wins() {
    let r = run_timeout(
        "exec sleep 20",
        false,
        b"",
        false,
        Duration::from_millis(100),
    );
    assert!(r.run.timed_out && !r.run.cancelled);
    assert!(r.decisions.is_empty());
    let r = run("exec sleep 20", false, b"", true);
    assert!(r.run.cancelled && !r.run.timed_out);
    assert!(r.decisions.is_empty());
}
