use super::{fixtures::*, *};
use crate::events::{BlindReason, MAX_LINE};

#[test]
fn closed_stdin_210_capture() {
    let mut p = OpencodeParser::new(0, Some(180), CAPTURE_VERSION, CAPTURE_VERSION);
    let lines: Vec<_> = CLOSED_STDIN.split_inclusive('\n').collect();
    assert_eq!(lines.len(), ARRIVAL_MS.len());
    let signals: Vec<_> = lines
        .iter()
        .zip(ARRIVAL_MS)
        .flat_map(|(line, &ms)| p.feed(line.as_bytes(), ms))
        .collect();
    assert_eq!(signals[0].kind, Kind::Request);
    assert_eq!(signals[0].ts_ms, 4001);
    assert!(signals
        .iter()
        .any(|s| s.ts_ms == 6266 && s.tool == Some((ToolClass::Read, Phase::Done))));
    assert_eq!(signals.len(), 13);
    assert_eq!(signals.last().unwrap().kind, Kind::Content);
    assert!(signals.iter().all(|s| s.ts_ms < 26741));
    assert_eq!(p.health().lines(), 17);
    assert_eq!(p.health().bad(), 0);
    assert!(p.end(26741).is_empty());
}

#[test]
fn version_drift_is_blind() {
    for (seen, expected) in [
        (CAPTURE_VERSION, None),
        ("2.1.1", Some(BlindReason::VersionDrift)),
    ] {
        let mut p = OpencodeParser::new(0, Some(180), CAPTURE_VERSION, seen);
        p.feed(CLOSED_STDIN.as_bytes(), 27000);
        assert_eq!(p.health.blind(27000), expected);
    }
}

#[test]
fn no_payload_retained() {
    let secret = ["sk", "-", "fake", "-", "private"].concat();
    let mut p = OpencodeParser::new(0, Some(180), CAPTURE_VERSION, CAPTURE_VERSION);
    for _ in 0..32 {
        let text = secret.repeat(1024);
        let line = format!("{{\"type\":\"text\",\"part\":{{\"text\":\"{text}\"}}}}\n");
        let signals = p.feed(line.as_bytes(), 1);
        assert_eq!(signals.len(), 1);
        assert_eq!(signals[0].kind, Kind::Content);
        assert!(!format!("{signals:?}").contains(&secret));
        assert!(p.framer.finish().is_none());
    }
    assert!(p.feed(&vec![b'x'; MAX_LINE + 1], 2).is_empty());
    assert!(p.end(2).is_empty());
    assert!(p.end(2).is_empty());
    assert_eq!(p.health().lines(), 33);

    // The projected capture has only "OK" text, so supplement it with unique payloads.
    const EXTRA: &str = "{\"type\":\"text\",\"part\":{\"text\":\"rl5b-text-payload-7c93\",\"reasoning\":\"rl5b-reasoning-payload-a821\",\"state\":{\"output\":\"rl5b-tool-output-payload-f604\"}}}\n";
    let mut parser = OpencodeParser::new(0, Some(180), CAPTURE_VERSION, CAPTURE_VERSION);
    for (line, ms) in CLOSED_STDIN
        .split_inclusive('\n')
        .zip(ARRIVAL_MS.iter().copied())
        .chain(std::iter::once((EXTRA, 27000)))
    {
        let signals = parser.feed(line.as_bytes(), ms);
        let state = format!("{parser:?}");
        let signals = format!("{signals:?}");
        for payload in [
            "OK",
            "rl5b-text-payload-7c93",
            "rl5b-reasoning-payload-a821",
            "rl5b-tool-output-payload-f604",
        ] {
            assert!(!state.contains(payload), "parser retained {payload}");
            assert!(!signals.contains(payload), "signals retained {payload}");
        }
    }
}
