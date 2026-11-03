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
    let d = raw_tick(m, now);
    if d.is_some_and(|d| matches!(d.act, Act::Warn | Act::WouldWarn)) {
        raw_tick(m, now)
    } else {
        d
    }
}
fn raw_tick(m: &mut Monitor, now: u64) -> Option<Decision> {
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
#[test]
fn stale_first_signal_clamps_to_t0_and_huge_limits_never_kill() {
    let modes = Modes {
        first: Mode::On,
        silent: Mode::On,
        milestone: Mode::On,
    };
    let mut m = Monitor::new(Role::Implement, &Limits::default(), modes, 10_000);
    signal(&mut m, Kind::Content, 5);
    assert_eq!(m.first_activity_ms(), Some(10_000));
    let huge = Limits {
        first_activity_s: Some(u64::MAX),
        silence_s: Some(u64::MAX),
        ..Limits::default()
    };
    let mut m = Monitor::new(Role::Implement, &huge, modes, u64::MAX - 1);
    assert_eq!(tick(&mut m, u64::MAX), None);
    signal(&mut m, Kind::Content, u64::MAX);
    assert_eq!(tick(&mut m, u64::MAX), None);
}
#[test]
fn per_rule_modes_are_independent() {
    let modes = Modes {
        first: Mode::Off,
        silent: Mode::On,
        milestone: Mode::Off,
    };
    let mut m = Monitor::new(Role::Implement, &Limits::default(), modes, 0);
    assert_eq!(tick(&mut m, 1_000_000), None);
    signal(&mut m, Kind::Content, 1_000_000);
    let d = tick(&mut m, 1_603_000).unwrap();
    assert_eq!(d.act, Act::Kill);
    decision(d, Silent, HangSilent, 0, 600);
    assert_eq!(m.state(), State::Ended(Silent));
}

#[test]
fn f_warns_once_at_80_percent() {
    let mut m = monitor(Limits::default(), Mode::On);
    assert_eq!(raw_tick(&mut m, 143_999), None);
    let d = raw_tick(&mut m, 144_000).unwrap();
    decision(d, FirstActivity, HangNoActivity, 0, 600);
    assert_eq!((d.act, d.kill_at_ms), (Act::Warn, Some(183_000)));
    assert_eq!(m.state(), State::Pending);
    assert_eq!(raw_tick(&mut m, 182_999), None);
    assert_eq!(raw_tick(&mut m, 183_000).unwrap().act, Act::Kill);
}

#[test]
fn s_warns_once_at_80_percent_of_s_eff() {
    for open in [false, true] {
        let mut m = monitor(Limits::default(), Mode::On);
        signal(&mut m, Kind::Content, 1000);
        if open {
            tool(&mut m, Phase::Start, 1000);
        }
        let seconds = if open { 1800 } else { 600 };
        let warn_at = 1000 + seconds * 800;
        let kill_at = 1000 + (seconds + 3) * 1000;
        assert_eq!(raw_tick(&mut m, warn_at - 1), None);
        let d = raw_tick(&mut m, warn_at).unwrap();
        decision(d, Silent, HangSilent, u32::from(open), seconds);
        assert_eq!((d.act, d.kill_at_ms), (Act::Warn, Some(kill_at)));
        assert_eq!(m.state(), State::Active);
        assert_eq!(raw_tick(&mut m, kill_at - 1), None);
        assert_eq!(raw_tick(&mut m, kill_at).unwrap().act, Act::Kill);
    }
}

#[test]
fn request_in_confirm_window_cancels_f_kill() {
    for at in [182_000, 182_999] {
        let mut m = monitor(Limits::default(), Mode::On);
        assert_eq!(raw_tick(&mut m, 144_000).unwrap().act, Act::Warn);
        signal(&mut m, Kind::Request, at);
        assert_eq!(raw_tick(&mut m, 183_000), None);
        assert_eq!(m.state(), State::Active);
    }
}

#[test]
fn strong_in_confirm_window_cancels_s_kill_and_rearms_warn() {
    for kind in [Kind::Content, Kind::Weak, Kind::Request] {
        let mut m = monitor(Limits::default(), Mode::On);
        signal(&mut m, Kind::Content, 0);
        assert_eq!(raw_tick(&mut m, 480_000).unwrap().act, Act::Warn);
        signal(&mut m, kind, 602_999);
        assert_eq!(raw_tick(&mut m, 603_000), None);
        assert_eq!(m.state(), State::Active);
        assert_eq!(raw_tick(&mut m, 1_082_998), None);
        assert_eq!(raw_tick(&mut m, 1_082_999).unwrap().act, Act::Warn);
    }
}

#[test]
fn late_tick_gives_warn_then_kill_and_carries_kill_at() {
    for active in [false, true] {
        for mode in [Mode::On, Mode::Shadow] {
            let mut m = monitor(Limits::default(), mode);
            if active {
                signal(&mut m, Kind::Content, 0);
            }
            let before = m.state();
            let d = raw_tick(&mut m, 900_000).unwrap();
            let shadow = mode == Mode::Shadow;
            assert_eq!(d.act, if shadow { Act::WouldWarn } else { Act::Warn });
            assert_eq!(d.kill_at_ms, Some(if active { 603_000 } else { 183_000 }));
            assert_eq!(m.state(), before);
            let kill = raw_tick(&mut m, 900_000).unwrap();
            assert_eq!(kill.act, if shadow { Act::WouldKill } else { Act::Kill });
            assert_eq!(kill, Decision { act: kill.act, ..d });
            assert_eq!(raw_tick(&mut m, 900_001), None);
            if shadow {
                assert_eq!(m.state(), before);
            }
        }
    }
}

#[test]
fn off_mode_error_signal_and_ended_monitor_give_no_warn() {
    let mut off = monitor(Limits::default(), Mode::Off);
    assert_eq!(raw_tick(&mut off, u64::MAX), None);
    signal(&mut off, Kind::Content, 0);
    assert_eq!(raw_tick(&mut off, u64::MAX), None);
    let mut m = monitor(Limits::default(), Mode::On);
    signal(&mut m, Kind::Error, 1000);
    assert_eq!(raw_tick(&mut m, 1000), None);
    assert_eq!(m.state(), State::Pending);
    assert_eq!(raw_tick(&mut m, 183_000).unwrap().act, Act::Warn);
    assert_eq!(raw_tick(&mut m, 183_000).unwrap().act, Act::Kill);
    signal(&mut m, Kind::Content, 184_000);
    assert_eq!(raw_tick(&mut m, u64::MAX), None);
}
