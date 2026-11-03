use super::*;

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
    assert_eq!(m.first_activity_ms(), None);
    assert_eq!(m.last_signal_ms(), None);
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
fn active_tool_phases_and_effective_silence() {
    let mut m = monitor(Limits::default(), Mode::On);
    for phase in [Phase::Start, Phase::Start, Phase::Done, Phase::End] {
        tool(&mut m, phase, 0);
    }
    assert_eq!(m.open_tools(), 1);
    assert_eq!(m.s_eff_s(), Some(1800));
    tool(&mut m, Phase::End, 0);
    tool(&mut m, Phase::End, 0);
    assert_eq!((m.open_tools(), m.s_eff_s()), (0, Some(600)));
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
}
