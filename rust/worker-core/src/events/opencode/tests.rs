use super::*;
use crate::activity::{Limits, Mode, Modes, Monitor};
use crate::hosts::Role;

fn parser() -> OpencodeParser {
    OpencodeParser::new(0, Some(180), "2.0.32", "2.0.32")
}
fn shapes(signals: Vec<Signal>) -> Vec<(Kind, Option<(ToolClass, Phase)>)> {
    signals.into_iter().map(|s| (s.kind, s.tool)).collect()
}
#[test]
fn historical_recordings() {
    use fixtures::*;
    use Kind::*;
    let request = (Request, None);
    let content = (Content, None);
    let edit = (Tool, Some((ToolClass::Edit, Phase::Done)));
    let denied = (Tool, Some((ToolClass::Other, Phase::Done)));
    for (raw, expected) in [
        (QUOTA, vec![request, (Error, None)]),
        (DEEPSEEK, vec![request, (Error, None)]),
        (TRANSPORT, vec![request, (Error, None)]),
        (TEXT, vec![request, content]),
        (TOOL, vec![request, edit, request, content]),
        (DENIED, vec![request, denied, edit, request, content]),
        (EMPTY, vec![request]),
    ] {
        let mut p = parser();
        assert_eq!(shapes(p.feed(raw.as_bytes(), 10)), expected);
        assert_eq!(p.health().lines(), raw.lines().count() as u64);
        assert_eq!(p.health().unknown(), 0);
        assert!(p.end(11).is_empty());
    }
}
#[test]
fn tool_classes_and_completion() {
    use ToolClass::*;
    let modes = Modes {
        first: Mode::Shadow,
        silent: Mode::Shadow,
        milestone: Mode::Shadow,
    };
    let mut monitor = Monitor::new(Role::Implement, &Limits::default(), modes, 0);
    for (name, class) in [
        ("read", Read),
        ("grep", Read),
        ("glob", Read),
        ("list", Read),
        ("bash", Exec),
        ("shell", Exec),
        ("edit", Edit),
        ("write", Edit),
        ("patch", Edit),
        ("READ", Other),
        ("unknown", Other),
        ("", Other),
    ] {
        for status in ["completed", "error", "running", "pending"] {
            let tool = if name.is_empty() {
                String::new()
            } else {
                format!("\"tool\":\"{name}\",")
            };
            let raw = format!("{{\"type\":\"tool_use\",\"part\":{{{tool}\"state\":{{\"status\":\"{status}\"}}}}}}\n");
            let mut p = parser();
            let signals = p.feed(raw.as_bytes(), 1);
            if matches!(status, "running" | "pending") {
                assert!(signals.is_empty());
            } else {
                let class = if status == "error" { Other } else { class };
                assert_eq!(
                    shapes(signals.clone()),
                    vec![(Kind::Tool, Some((class, Phase::Done)))]
                );
                monitor.feed(signals[0]);
                assert_eq!(monitor.open_tools(), 0);
            }
            assert_eq!(p.health().unknown(), 0);
        }
    }
}
#[test]
fn malformed_and_unknown() {
    let mut p = parser();
    assert!(p.feed(b"\xff\nnot json\n[]\n1\nnull\n", 1).is_empty());
    for raw in [
        "{}",
        r#"{"type":"new"}"#,
        r#"{"type":"text"}"#,
        r#"{"type":"error","error":null}"#,
        r#"{"type":"tool_use","part":{}}"#,
        r#"{"type":"step_start","part":[]}"#,
        r#"{"type":"text","part":{"text":1}}"#,
    ] {
        assert_eq!(
            shapes(p.feed(format!("{raw}\n").as_bytes(), 2)),
            vec![(Kind::Request, None)]
        );
    }
    for raw in [
        r#"{"type":"text","part":{"text":" \t"}}"#,
        r#"{"type":"reasoning","part":{}}"#,
        r#"{"type":"step_finish","part":{}}"#,
    ] {
        assert!(p.feed(format!("{raw}\n").as_bytes(), 3).is_empty());
    }
    assert_eq!(
        (p.health().bad(), p.health().unknown(), p.health().parsed()),
        (5, 7, 10)
    );
    assert_eq!(
        shapes(p.feed(
            b"{\"type\":\"reasoning\",\"part\":{\"text\":\"think\"}}\n",
            4
        )),
        vec![(Kind::Content, None)]
    );
}
#[test]
fn chunk_split_and_health() {
    let raw = fixtures::TOOL.replace('\n', "\r\n");
    let mut whole = parser();
    let expected = shapes(whole.feed(raw.as_bytes(), 7));
    for split in 0..=raw.len() {
        let mut p = parser();
        let mut signals = p.feed(&raw.as_bytes()[..split], 7);
        signals.extend(p.feed(&raw.as_bytes()[split..], 7));
        assert_eq!(shapes(signals), expected);
        assert_eq!((p.health().lines(), p.health().parsed()), (5, 5));
    }
    let mut p = parser();
    assert!(p
        .feed(&vec![b'x'; super::super::MAX_LINE + 1], 1)
        .is_empty());
    assert_eq!(p.feed(b"\n{}\r\n\r\n", 2).len(), 1);
    assert_eq!(
        (p.health().lines(), p.health().bad(), p.health().unknown()),
        (2, 1, 1)
    );
    assert!(p
        .feed(b"{\"type\":\"step_start\",\"part\":{}}", 3)
        .is_empty());
    assert_eq!(p.end(4)[0].ts_ms, 4);
    assert!(p.end(5).is_empty());
    for _ in 0..19 {
        p.feed(b"bad\n", 6);
    }
    assert_eq!(p.health.blind(6), Some(super::super::BlindReason::BadRatio));
}
#[test]
fn receive_time_only() {
    let mut p = parser();
    for wire in [0, u64::MAX] {
        let raw = format!("{{\"type\":\"step_start\",\"timestamp\":{wire},\"part\":{{}}}}\n");
        assert_eq!(p.feed(raw.as_bytes(), 42)[0].ts_ms, 42);
    }
}
