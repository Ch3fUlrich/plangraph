use super::*;
use crate::{activity::*, hosts::Role};

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
