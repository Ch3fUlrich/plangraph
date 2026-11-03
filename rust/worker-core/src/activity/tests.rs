use super::*;
use KillOutcome::{HangNoActivity, HangSilent};
use Rule::{FirstActivity, Silent};

fn monitor(limits: Limits, mode: Mode) -> Monitor {
    Monitor::new(
        Role::Implement,
        &limits,
        Modes {
            first: mode,
            silent: mode,
            milestone: mode,
        },
        0,
    )
}
fn signal(m: &mut Monitor, kind: Kind, ts_ms: u64) {
    m.feed(Signal {
        kind,
        ts_ms,
        tool: None,
    });
}
fn tick(m: &mut Monitor, now: u64) -> Option<Decision> {
    m.tick(now, Ctx::default())
}
fn decision(d: Decision, rule: Rule, outcome: KillOutcome, open: u32, seconds: u64) {
    assert_eq!(d.rule, rule);
    assert_eq!(d.outcome, outcome);
    assert_eq!(d.open_tools, open);
    assert_eq!(d.s_eff_s, Some(seconds));
}
fn tool(m: &mut Monitor, phase: Phase, ts_ms: u64) {
    m.feed(Signal {
        kind: Kind::Tool,
        ts_ms,
        tool: Some((ToolClass::Read, phase)),
    });
}

#[test]
fn pending_request_and_each_strong_activate() {
    for kind in [Kind::Request, Kind::Content, Kind::Tool, Kind::Verdict] {
        let mut m = monitor(Limits::default(), Mode::On);
        signal(&mut m, kind, 42);
        assert_eq!(m.state(), State::Active);
        assert_eq!(m.first_activity_ms(), Some(42));
        assert_eq!(m.last_signal_ms(), Some(42));
        assert_eq!(m.last_strong_ms(), kind.is_strong().then_some(42));
        signal(&mut m, Kind::Request, 43);
        assert_eq!(
            m.last_signal_ms(),
            Some(if kind.is_strong() { 43 } else { 42 })
        );
    }
}
#[test]
fn pending_weak_never_meets_first_activity() {
    let mut m = monitor(Limits::default(), Mode::On);
    for second in 0..=183 {
        signal(&mut m, Kind::Weak, second * 1000);
    }
    assert_eq!(m.state(), State::Pending);
    assert_eq!(tick(&mut m, 182_999), None);
    let d = tick(&mut m, 183_000).unwrap();
    decision(d, FirstActivity, HangNoActivity, 0, 600);
}
#[test]
fn active_strong_weak_request_and_error_rows() {
    let mut m = monitor(Limits::default(), Mode::On);
    signal(&mut m, Kind::Request, 10);
    signal(&mut m, Kind::Request, 20);
    assert_eq!(m.last_signal_ms(), Some(10));
    signal(&mut m, Kind::Content, 30);
    assert_eq!(m.last_strong_ms(), Some(30));
    signal(&mut m, Kind::Request, 40);
    assert_eq!(m.last_signal_ms(), Some(40));
    signal(&mut m, Kind::Weak, 50);
    signal(&mut m, Kind::Request, 60);
    signal(&mut m, Kind::Error, 70);
    signal(&mut m, Kind::Weak, 1);
    assert_eq!(m.last_signal_ms(), Some(50));
    assert_eq!(m.last_strong_ms(), Some(30));
    let mut pending = monitor(Limits::default(), Mode::On);
    signal(&mut pending, Kind::Error, 99);
    assert_eq!(pending.state(), State::Pending);
}
#[test]
fn active_tool_phases_and_silent_thresholds() {
    for open in [false, true] {
        let mut m = monitor(Limits::default(), Mode::On);
        signal(&mut m, Kind::Content, 0);
        if open {
            tool(&mut m, Phase::Start, 0);
        }
        let seconds = if open { 1800 } else { 600 };
        assert_eq!(tick(&mut m, (seconds + 3) * 1000 - 1), None);
        let d = tick(&mut m, (seconds + 3) * 1000).unwrap();
        decision(d, Silent, HangSilent, u32::from(open), seconds);
    }
    let mut m = monitor(Limits::default(), Mode::On);
    for phase in [Phase::Start, Phase::Start, Phase::Done, Phase::End] {
        tool(&mut m, phase, 0);
    }
    assert_eq!(m.open_tools(), 1);
    assert_eq!(m.s_eff_s(), Some(1800));
    tool(&mut m, Phase::End, 0);
    tool(&mut m, Phase::End, 0);
    assert_eq!((m.open_tools(), m.s_eff_s()), (0, Some(600)));
    assert!(tick(&mut m, 603_000).is_some());
}
#[test]
fn absent_and_null_limits_disable_rules() {
    let limits: Limits = serde_json::from_str(r#"{"first_activity_s":1}"#).unwrap();
    let empty: Limits = serde_json::from_str(r#"{"first_activity_s":null}"#).unwrap();
    assert_eq!(empty.first_activity_s, None);
    assert_eq!(
        limits,
        Limits {
            first_activity_s: Some(1),
            ..empty
        }
    );
    for value in [
        limits.silence_s,
        limits.milestone_s,
        limits.tool_max_s,
        limits.confirm_s,
        limits.cpu_active_ms,
    ] {
        assert_eq!(value, None);
    }
    let mut m = monitor(empty, Mode::On);
    assert_eq!(tick(&mut m, u64::MAX), None);
    signal(&mut m, Kind::Content, 0);
    tool(&mut m, Phase::Start, 0);
    assert_eq!(tick(&mut m, u64::MAX), None);
    let mut m = monitor(
        Limits {
            tool_max_s: None,
            ..Limits::default()
        },
        Mode::On,
    );
    tool(&mut m, Phase::Start, 0);
    assert_eq!(tick(&mut m, u64::MAX), None);
    let mut m = monitor(limits, Mode::On);
    assert!(tick(&mut m, 1000).is_some());
}
#[test]
fn off_shadow_and_on_episode_semantics() {
    let mut off = monitor(Limits::default(), Mode::Off);
    assert_eq!(tick(&mut off, u64::MAX), None);
    signal(&mut off, Kind::Content, 0);
    assert_eq!(tick(&mut off, u64::MAX), None);
    let mut m = monitor(Limits::default(), Mode::Shadow);
    assert_eq!(tick(&mut m, 183_000).unwrap().act, Act::WouldKill);
    assert_eq!(tick(&mut m, 184_000), None);
    signal(&mut m, Kind::Verdict, 200_000);
    assert_eq!(tick(&mut m, 803_000).unwrap().act, Act::WouldKill);
    signal(&mut m, Kind::Weak, 200_000);
    assert_eq!(tick(&mut m, 804_000), None);
    signal(&mut m, Kind::Weak, 804_000);
    assert_eq!(tick(&mut m, 1_407_000).unwrap().act, Act::WouldKill);
    let mut m = monitor(Limits::default(), Mode::On);
    assert_eq!(tick(&mut m, 183_000).unwrap().act, Act::Kill);
    signal(&mut m, Kind::Content, 184_000);
    assert_eq!(m.state(), State::Ended(Rule::FirstActivity));
    assert_eq!(m.first_activity_ms(), None);
    assert_eq!(tick(&mut m, u64::MAX), None);
}
