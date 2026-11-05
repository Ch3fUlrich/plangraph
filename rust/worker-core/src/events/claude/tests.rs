use super::*;
use crate::activity::{Limits, Mode, Modes, Monitor, State};
use crate::hosts::Role;
use serde_json::{json, Value};
pub(super) const TOOL: &str = include_str!("../../../tests/fixtures/claude/claude-tool-run.jsonl");
pub(super) const TEXT: &str = include_str!("../../../tests/fixtures/claude/claude-text-run.jsonl");
pub(super) const QODER: &str = include_str!("../../../tests/fixtures/claude/qodercli-run.jsonl");
pub(super) const README: &str = include_str!("../../../tests/fixtures/claude/README.md");
pub(super) const ARRIVALS: [&str; 3] = [
    include_str!("../../../tests/fixtures/claude/claude-tool-run.arrival"),
    include_str!("../../../tests/fixtures/claude/claude-text-run.arrival"),
    include_str!("../../../tests/fixtures/claude/qodercli-run.arrival"),
];
pub(super) fn parser(flavor: Flavor) -> ClaudeParser {
    ClaudeParser::new(
        flavor,
        0,
        Some(180),
        if flavor == Flavor::Claude {
            RECORDED_CLAUDE[0]
        } else {
            RECORDED_QODERCLI[0]
        },
    )
}
pub(super) fn kinds(signals: &[Signal]) -> Vec<(Kind, Option<(ToolClass, Phase)>)> {
    signals.iter().map(|s| (s.kind, s.tool)).collect()
}
pub(super) fn send(p: &mut ClaudeParser, v: Value) -> Vec<Signal> {
    p.feed(format!("{v}\n").as_bytes(), 10)
}
pub(super) fn check(v: Value, kind: Kind) {
    assert_eq!(
        kinds(&send(&mut parser(Flavor::Claude), v)),
        vec![(kind, None)]
    );
}
pub(super) fn monitor() -> Monitor {
    Monitor::new(
        Role::Implement,
        &Limits::default(),
        Modes {
            first: Mode::On,
            silent: Mode::On,
            milestone: Mode::On,
        },
        0,
    )
}
pub(super) fn arrivals(s: &str) -> Vec<u64> {
    s.trim().split(',').map(|n| n.parse().unwrap()).collect()
}
#[test]
fn claude_hook_started_hook_response_and_init_are_not_activity() {
    let mut p = parser(Flavor::Claude);
    let mut m = monitor();
    for subtype in ["hook_started", "hook_response", "init"] {
        let signals = send(&mut p, json!({"type":"system", "subtype":subtype}));
        assert!(signals.is_empty());
        for s in signals {
            m.feed(s);
        }
    }
    assert_eq!(m.state(), State::Pending);
}
#[test]
fn claude_status_requesting_is_request() {
    check(
        json!({"type":"system","subtype":"status","status":"requesting"}),
        Kind::Request,
    );
    assert!(send(
        &mut parser(Flavor::Claude),
        json!({"type":"system","subtype":"status","status":"idle"})
    )
    .is_empty());
}
#[test]
fn claude_message_start_is_request() {
    check(
        json!({"type":"stream_event","event":{"type":"message_start"}}),
        Kind::Request,
    );
}
#[test]
fn claude_assistant_text_and_thinking_are_content() {
    let v = json!({"type":"assistant","message":{"content":[{"type":"text"},{"type":"thinking"}]}});
    assert_eq!(
        kinds(&send(&mut parser(Flavor::Claude), v)),
        vec![(Kind::Content, None); 2]
    );
    check(
        json!({"type":"assistant","message":{"content":"words"}}),
        Kind::Content,
    );
}
#[test]
fn claude_tool_error_result_is_other_end() {
    let mut p = parser(Flavor::Claude);
    send(
        &mut p,
        json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"a","name":"Edit"}]}}),
    );
    assert_eq!(
        kinds(&send(
            &mut p,
            json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"a","is_error":true}]}})
        )),
        vec![(Kind::Tool, Some((ToolClass::Other, Phase::End)))]
    );
}
#[test]
fn claude_result_is_request_even_when_is_error() {
    check(json!({"type":"result","is_error":true}), Kind::Request);
}
#[test]
fn claude_result_safety_stops_is_error() {
    let mut v: Value = serde_json::from_str(TEXT.lines().last().unwrap()).unwrap();
    v["safety_stops"] = json!(1);
    check(v, Kind::Error);
}
#[test]
fn claude_unknown_top_level_type_is_request() {
    for v in [
        json!({"type":"new"}),
        json!({}),
        json!({"type":3}),
        json!({"type":"stream_event"}),
    ] {
        check(v, Kind::Request);
    }
}
#[test]
fn claude_unknown_system_subtype_is_not_activity() {
    for v in [
        json!({"type":"system","subtype":"new"}),
        json!({"type":"system"}),
        json!({"type":"system","subtype":"thinking_tokens"}),
    ] {
        assert!(send(&mut parser(Flavor::Claude), v).is_empty());
    }
}
#[test]
fn qodercli_init_is_request_and_claude_init_is_not() {
    let v = json!({"type":"system","subtype":"init"});
    assert!(send(&mut parser(Flavor::Claude), v.clone()).is_empty());
    assert_eq!(
        kinds(&send(&mut parser(Flavor::Qodercli), v)),
        vec![(Kind::Request, None)]
    );
}
#[test]
fn qodercli_unknown_tool_name_is_other() {
    for name in [json!("read"), Value::Null] {
        assert_eq!(
            kinds(&send(
                &mut parser(Flavor::Qodercli),
                json!({"type":"assistant","message":{"content":[{"type":"tool_use","name":name}]}})
            )),
            vec![(Kind::Tool, Some((ToolClass::Other, Phase::Start)))]
        );
    }
}
#[test]
fn claude_captures_parse_without_bad_lines() {
    for (i, capture) in [TOOL, TEXT, QODER].iter().enumerate() {
        let mut p = parser(if i == 2 {
            Flavor::Qodercli
        } else {
            Flavor::Claude
        });
        let times = arrivals(ARRIVALS[i]);
        assert_eq!(times.len(), capture.lines().count());
        let signals: Vec<_> = capture
            .lines()
            .zip(times)
            .flat_map(|(line, t)| p.feed(format!("{line}\n").as_bytes(), t))
            .collect();
        assert_eq!(p.health.bad(), 0);
        assert_eq!(p.health.lines(), capture.lines().count() as u64);
        assert_eq!(p.health.blind(20000), None);
        if i == 0 {
            for phase in [Phase::Start, Phase::End] {
                assert!(signals
                    .iter()
                    .any(|s| s.tool == Some((ToolClass::Read, phase))));
            }
        }
    }
}
#[test]
fn qodercli_events_arrive_before_exit() {
    let a = arrivals(ARRIVALS[2]);
    assert!(a.last().unwrap() - a.first().unwrap() >= 1000);
    assert!(README.lines().any(|l| l == "qodercli_stream: live"));
}
#[test]
fn readme_gap_fixes_the_claude_silence_entry() {
    let gap = ARRIVALS[..2]
        .iter()
        .flat_map(|s| {
            arrivals(s)
                .windows(2)
                .map(|p| p[1] - p[0])
                .collect::<Vec<_>>()
        })
        .max()
        .unwrap();
    assert!(README
        .lines()
        .any(|l| l == format!("largest_gap_ms: {gap}")));
    let full = README
        .lines()
        .find_map(|l| l.strip_prefix("full_capture_gap_ms: "))
        .map(|n| n.parse::<u64>().unwrap())
        .unwrap_or(0);
    let silence = if gap.max(full) < 90000 { 180 } else { 600 };
    assert!(README.lines().any(|l| l == format!("silence_s: {silence}")));
    for (key, versions) in [
        ("claude_version", RECORDED_CLAUDE),
        ("qodercli_version", RECORDED_QODERCLI),
    ] {
        for v in versions {
            assert!(README.lines().any(|l| l == format!("{key}: {v}")));
        }
    }
}
