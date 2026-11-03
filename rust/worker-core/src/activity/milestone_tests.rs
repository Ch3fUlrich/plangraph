use super::tests::{decision, progress, progress_tick, raw_tick, signal};
use super::*;
use Rule::Silent;

#[test]
fn w_warns_once_at_60_percent_of_m_and_m_kills_when_safe_alt() {
    for mode in [Mode::On, Mode::Shadow] {
        let mut m = progress(mode);
        assert_eq!(progress_tick(&mut m, 360_999, true), None);
        let d = progress_tick(&mut m, 361_000, true).unwrap();
        decision(d, Rule::Milestone, KillOutcome::NoProgress, 0, 600);
        assert_eq!(d.kill_at_ms, Some(601_000));
        assert_eq!(
            d.act,
            if mode == Mode::On {
                Act::Warn
            } else {
                Act::WouldWarn
            }
        );
        assert_eq!(m.state(), State::Active);
        assert_eq!(progress_tick(&mut m, 600_999, true), None);
        let k = progress_tick(&mut m, 601_000, true).unwrap();
        assert_eq!(
            k,
            Decision {
                act: if mode == Mode::On {
                    Act::Kill
                } else {
                    Act::WouldKill
                },
                ..d
            }
        );
        assert_eq!(
            m.state(),
            if mode == Mode::On {
                State::Ended(Rule::Milestone)
            } else {
                State::Active
            }
        );
        assert_eq!(progress_tick(&mut m, 1_201_000, true), None);
    }
}
#[test]
fn safe_alt_false_gives_exactly_one_m_warning_over_two_m_periods() {
    let mut m = progress(Mode::On);
    assert_eq!(
        progress_tick(&mut m, 361_000, false).unwrap().act,
        Act::Warn
    );
    for now in [601_000, 901_000, 1_201_000] {
        assert_eq!(progress_tick(&mut m, now, false), None);
        assert_eq!(m.state(), State::Active);
    }
    assert_eq!(progress_tick(&mut m, 1_202_000, true), None);
    assert_eq!(raw_tick(&mut m, 1_682_000).unwrap().rule, Silent);
    assert_eq!(raw_tick(&mut m, 1_805_000).unwrap().act, Act::Kill);
}
#[test]
fn safe_alt_latched_at_first_m_tick_even_when_s_warns() {
    let mut m = progress(Mode::On);
    assert_eq!(raw_tick(&mut m, 601_000).unwrap().rule, Silent);
    assert_eq!(progress_tick(&mut m, 601_001, true).unwrap().act, Act::Warn);
    assert_eq!(progress_tick(&mut m, 601_002, true), None);
    assert_eq!(m.state(), State::Active);
}
#[test]
fn edited_writer_not_ended_by_m_and_milestone_keeps_s_rule() {
    for mode in [Mode::Off, Mode::Shadow, Mode::On] {
        for source in 0..4 {
            let mut m = progress(mode);
            if source == 0 {
                m.tick(
                    2000,
                    Ctx {
                        edited: true,
                        safe_alt: true,
                    },
                );
            } else if source == 3 {
                signal(&mut m, Kind::Verdict, 2000);
            } else {
                m.feed(Signal {
                    kind: Kind::Tool,
                    ts_ms: 2000,
                    tool: Some((
                        ToolClass::Edit,
                        if source == 1 { Phase::End } else { Phase::Done },
                    )),
                });
            }
            m.modes.milestone = Mode::On;
            m.modes.silent = Mode::On;
            assert_eq!(progress_tick(&mut m, 601_000, true), None);
            assert_eq!(progress_tick(&mut m, 1_201_000, true), None);
            assert_eq!(raw_tick(&mut m, 1_681_000).unwrap().rule, Silent);
            assert_eq!(raw_tick(&mut m, 1_804_000).unwrap().act, Act::Kill);
        }
    }
    let mut m = progress(Mode::On);
    m.feed(Signal {
        kind: Kind::Tool,
        ts_ms: 2000,
        tool: Some((ToolClass::Edit, Phase::Start)),
    });
    assert_eq!(
        progress_tick(&mut m, 361_000, true).unwrap().rule,
        Rule::Milestone
    );
}
#[test]
fn milestone_s_none_is_m_off_and_m_mode_off() {
    for null_limit in [false, true] {
        let mut m = progress(Mode::On);
        if null_limit {
            m.limits.milestone_s = None;
        } else {
            m.modes.milestone = Mode::Off;
        }
        for now in (100_000..=3_000_000).step_by(100_000) {
            assert_eq!(progress_tick(&mut m, now, true), None);
            assert_eq!(m.state(), State::Active);
        }
    }
}
#[test]
fn m_late_tick_warns_before_kill_and_kills_precede_warnings() {
    let mut m = progress(Mode::On);
    assert_eq!(progress_tick(&mut m, 601_000, true).unwrap().act, Act::Warn);
    let d = m
        .tick(
            1_081_000,
            Ctx {
                edited: false,
                safe_alt: true,
            },
        )
        .unwrap();
    assert_eq!((d.rule, d.act), (Rule::Milestone, Act::Kill));
    let mut m = progress(Mode::On);
    assert_eq!(progress_tick(&mut m, 361_000, true).unwrap().act, Act::Warn);
    assert_eq!(raw_tick(&mut m, 841_000).unwrap().rule, Silent);
    let d = m
        .tick(
            964_000,
            Ctx {
                edited: false,
                safe_alt: true,
            },
        )
        .unwrap();
    assert_eq!((d.rule, d.act), (Silent, Act::Kill));
}

#[test]
fn m_runs_without_s_and_late_unsafe_warn_is_once() {
    for safe in [false, true] {
        let mut m = progress(Mode::On);
        m.limits.silence_s = None;
        let ctx = Ctx {
            edited: false,
            safe_alt: safe,
        };
        assert_eq!(m.tick(601_000, ctx).unwrap().act, Act::Warn);
        let next = m.tick(
            601_001,
            Ctx {
                safe_alt: true,
                ..ctx
            },
        );
        if safe {
            assert_eq!(next.unwrap().act, Act::Kill);
        } else {
            assert_eq!(next, None);
            assert_eq!(m.state(), State::Active);
        }
    }
}
