use super::*;

#[test]
fn framer_whole_lines_and_crlf() {
    assert_eq!(
        Framer::new().push(b"\n\r\na\r\nb\n"),
        vec![Line::Bytes(b"a".to_vec()), Line::Bytes(b"b".to_vec())]
    );
}
#[test]
fn framer_chunk_split_anywhere_equals_one_chunk() {
    let input = "aé\r\nb\n".as_bytes();
    let expected = Framer::new().push(input);
    for split in 0..=input.len() {
        let mut f = Framer::new();
        let mut got = f.push(&input[..split]);
        got.extend(f.push(&input[split..]));
        assert_eq!(got, expected);
    }
    let mut f = Framer::new();
    assert_eq!(
        input.iter().flat_map(|b| f.push(&[*b])).collect::<Vec<_>>(),
        expected
    );
}
#[test]
fn framer_finish_returns_unterminated_tail() {
    let mut f = Framer::new();
    assert!(f.push(b"tail\r").is_empty());
    assert_eq!(f.finish(), Some(b"tail".to_vec()));
    assert_eq!(f.finish(), None);
}
#[test]
fn framer_cap_keeps_exact_max_line_and_drops_longer_then_continues() {
    let mut f = Framer::new();
    let exact = vec![b'a'; MAX_LINE];
    assert!(f.push(&exact).is_empty());
    assert_eq!(f.buf.len(), MAX_LINE);
    assert_eq!(f.push(b"\n"), vec![Line::Bytes(exact.clone())]);
    assert!(f.push(&exact).is_empty());
    assert_eq!(f.push(b"xx"), vec![Line::Overlong]);
    assert!(f.buf.len() <= MAX_LINE);
    assert!(f.push(&exact).is_empty());
    assert_eq!(f.push(b"\nok\n"), vec![Line::Bytes(b"ok".to_vec())]);
    f.push(&vec![b'x'; MAX_LINE + 1]);
    assert_eq!(f.finish(), None);
}
struct Synthetic {
    framer: Framer,
    health: ChannelHealth,
}
impl Synthetic {
    fn new() -> Self {
        Self {
            framer: Framer::new(),
            health: ChannelHealth::new(0, Some(10)),
        }
    }
}
impl EventParser for Synthetic {
    fn feed(&mut self, chunk: &[u8], now_ms: u64) -> Vec<Signal> {
        let mut signals = Vec::new();
        for line in self.framer.push(chunk) {
            let object = match line {
                Line::Bytes(b) => event_object(&b),
                Line::Overlong => None,
            };
            let kind = match object {
                Some(v) if v["type"] == "known" => {
                    signals.push(Signal {
                        kind: Kind::Content,
                        ts_ms: now_ms,
                        tool: None,
                    });
                    LineKind::Known
                }
                Some(_) => {
                    signals.push(unknown_event(now_ms));
                    LineKind::Unknown
                }
                None => LineKind::NotJson,
            };
            self.health.line(kind, now_ms);
        }
        signals
    }
    fn health(&self) -> &ChannelHealth {
        &self.health
    }
}
#[test]
fn non_json_lines_are_ignored_and_counted() {
    let mut p = Synthetic::new();
    assert!(p.feed(b"garbage\n\xff\n\n", 1).is_empty());
    assert!(p.feed(&vec![b'x'; MAX_LINE + 1], 2).is_empty());
    assert_eq!(
        (p.health().lines(), p.health().bad(), p.health().parsed()),
        (3, 3, 0)
    );
}
#[test]
fn json_array_and_scalar_are_not_events() {
    for line in ["[]", "1", "null", "true", "\"text\"", "{"] {
        assert!(event_object(line.as_bytes()).is_none());
    }
}
#[test]
fn unknown_wellformed_event_is_request() {
    let mut p = Synthetic::new();
    let signals = p.feed(b"{}\n", 12);
    assert_eq!(signals[0].kind, Kind::Request);
    assert_eq!(signals[0].ts_ms, 12);
    assert!(signals[0].tool.is_none());
    assert_eq!((p.health().parsed(), p.health().unknown()), (1, 1));
}
#[test]
fn blind_when_lines_but_none_parses_within_f() {
    let mut h = ChannelHealth::new(100, Some(2));
    h.line(LineKind::NotJson, 101);
    assert_eq!(h.blind(2099), None);
    assert_eq!(h.blind(2100), Some(BlindReason::NoParseWithinF));
    let mut good = ChannelHealth::new(100, Some(2));
    good.line(LineKind::NotJson, 101);
    good.line(LineKind::Known, 2099);
    assert_eq!(good.blind(2100), None);
    let mut off = ChannelHealth::new(0, None);
    off.line(LineKind::NotJson, 1);
    assert_eq!(off.blind(100000), None);
}
#[test]
fn silence_alone_is_not_blind() {
    assert_eq!(ChannelHealth::new(0, Some(2)).blind(20000), None);
}
#[test]
fn blind_when_over_half_of_20_lines_bad() {
    for bad in [10, 11, 19] {
        let mut h = ChannelHealth::new(0, Some(10));
        for _ in 0..bad {
            h.line(LineKind::NotJson, 1);
        }
        assert_eq!(h.blind(1), None);
        if bad == 19 {
            continue;
        }
        for _ in bad..20 {
            h.line(LineKind::Known, 1);
        }
        assert_eq!(
            h.blind(1),
            if bad == 11 {
                Some(BlindReason::BadRatio)
            } else {
                None
            }
        );
    }
}
#[test]
fn version_drift_is_blind_and_equal_is_not() {
    let mut h = ChannelHealth::new(0, None);
    h.pin("1.0", "1.0");
    assert_eq!(h.blind(0), None);
    h.pin("1.0", "1.0 ");
    assert_eq!(h.blind(0), Some(BlindReason::VersionDrift));
}
#[test]
fn no_parser_is_blind_at_once() {
    assert_eq!(
        ChannelHealth::no_parser(0).blind(0),
        Some(BlindReason::NoParser)
    );
}
#[test]
fn blind_is_sticky_first_reason_wins() {
    let mut h = ChannelHealth::new(0, Some(1));
    h.line(LineKind::NotJson, 0);
    h.line(LineKind::Known, 1000);
    h.pin("1", "2");
    assert_eq!(h.blind(2000), Some(BlindReason::NoParseWithinF));
    let mut h = ChannelHealth::no_parser(0);
    h.pin("1", "2");
    assert_eq!(h.blind(0), Some(BlindReason::NoParser));
}
#[test]
fn synthetic_parser_composes() {
    let mut p = Synthetic::new();
    assert!(p.feed(b"{\"type\":", 1).is_empty());
    let signals = p.feed(b"\"known\"}\n{}\nbad\n", 2);
    assert_eq!(
        signals.iter().map(|s| s.kind).collect::<Vec<_>>(),
        vec![Kind::Content, Kind::Request]
    );
    assert_eq!(
        (
            p.health().lines(),
            p.health().parsed(),
            p.health().bad(),
            p.health().unknown()
        ),
        (3, 2, 1, 1)
    );
    assert_eq!(p.health.blind(10000), None);
}
