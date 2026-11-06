use super::*;
use crate::{activity::*, hosts::Role};

fn poll(s: &mut Sampler, now: u64, total: u64) -> Option<Signal> {
    s.poll(now, &mut || Ok(CpuSample { total_ms: total }))
}
fn monitor() -> Monitor {
    Monitor::new(
        Role::Implement,
        &Limits::default(),
        Modes {
            first: Mode::On,
            silent: Mode::Off,
            milestone: Mode::On,
        },
        0,
    )
}
fn content(m: &mut Monitor) {
    m.feed(Signal {
        kind: Kind::Content,
        ts_ms: 1,
        tool: None,
    });
}

#[test]
fn thirty_second_boundary() {
    let mut s = Sampler::new(1);
    assert!(poll(&mut s, 0, 0).is_none());
    assert!(s.poll(29_999, &mut || panic!("early read")).is_none());
    let signal = poll(&mut s, 30_000, 1).unwrap();
    assert_eq!(signal.kind, Kind::Weak);
    assert_eq!(signal.ts_ms, 30_000);
    assert!(signal.tool.is_none());
}
#[test]
fn threshold_equal_and_below() {
    let mut s = Sampler::new(10);
    poll(&mut s, 0, 0);
    assert!(poll(&mut s, 30_000, 9).is_none());
    assert!(poll(&mut s, 60_000, 19).is_some());
    let mut s = Sampler::new(0);
    poll(&mut s, 0, 0);
    assert!(poll(&mut s, 30_000, 0).is_none());
    assert!(poll(&mut s, 60_000, 1).is_some());
}
#[test]
fn initial_failure_counter_reset_clock_regression() {
    let mut s = Sampler::new(1);
    assert!(s
        .poll(0, &mut || Err(io::Error::other("missing")))
        .is_none());
    assert!(poll(&mut s, 30_000, 100).is_none());
    assert!(poll(&mut s, 60_000, 1).is_none());
    assert!(poll(&mut s, 90_000, 2).is_some());
    assert!(poll(&mut s, 1, 100).is_none());
    assert!(s
        .poll(30_001, &mut || Err(io::Error::other("denied")))
        .is_none());
    assert!(poll(&mut s, 60_001, 500).is_none());
}
#[test]
fn no_catchup_burst() {
    let mut s = Sampler::new(1);
    poll(&mut s, 0, 0);
    assert!(poll(&mut s, u64::MAX, u64::MAX).is_some());
    assert!(s.poll(u64::MAX, &mut || panic!("burst")).is_none());
}
#[test]
fn busy_refreshes_only_last_signal() {
    let mut m = monitor();
    content(&mut m);
    let mut s = Sampler::new(1);
    poll(&mut s, 0, 0);
    m.feed(poll(&mut s, 30_000, 20).unwrap());
    assert_eq!(m.last_signal_ms(), Some(30_000));
    assert_eq!(m.last_strong_ms(), Some(1));
    assert_eq!(m.first_activity_ms(), Some(1));
}
#[test]
fn idle_does_not_refresh() {
    let mut m = monitor();
    content(&mut m);
    let mut s = Sampler::new(1);
    poll(&mut s, 0, 5);
    if let Some(signal) = poll(&mut s, 30_000, 5) {
        m.feed(signal);
    }
    assert_eq!(m.last_signal_ms(), Some(1));
}
#[test]
fn weak_never_meets_first_activity_or_milestone() {
    let mut pending = monitor();
    let mut active = monitor();
    content(&mut active);
    let mut s = Sampler::new(1);
    poll(&mut s, 0, 0);
    for n in 1..=21 {
        let signal = poll(&mut s, n * 30_000, n).unwrap();
        pending.feed(signal);
        active.feed(signal);
    }
    assert_eq!(pending.state(), State::Pending);
    assert_eq!(pending.first_activity_ms(), None);
    assert_eq!(pending.last_signal_ms(), None);
    assert_eq!(
        pending.tick(630_000, Ctx::default()).unwrap().rule,
        Rule::FirstActivity
    );
    assert_eq!(
        active.tick(630_000, Ctx::default()).unwrap().rule,
        Rule::Milestone
    );
    assert_eq!(active.last_strong_ms(), Some(1));
    assert_eq!(active.first_activity_ms(), Some(1));
}
