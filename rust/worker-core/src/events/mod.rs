//! Pure event-channel primitives; bytes classify health, never activity.
use crate::activity::{Kind, Signal};
pub mod claude;
pub mod opencode;

#[cfg(test)]
mod tests;

pub const MAX_LINE: usize = 1 << 20;

#[derive(Debug, PartialEq, Eq)]
pub enum Line {
    Bytes(Vec<u8>),
    Overlong,
}

#[derive(Debug, Default)]
pub struct Framer {
    buf: Vec<u8>,
    dropping: bool,
}
impl Framer {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn push(&mut self, chunk: &[u8]) -> Vec<Line> {
        let mut lines = Vec::new();
        for &byte in chunk {
            if byte == b'\n' {
                if !self.dropping {
                    if let Some(line) = self.finish() {
                        lines.push(Line::Bytes(line));
                    }
                }
                self.dropping = false;
            } else if !self.dropping {
                if self.buf.len() == MAX_LINE {
                    self.buf.clear();
                    self.dropping = true;
                    lines.push(Line::Overlong);
                } else {
                    self.buf.push(byte);
                }
            }
        }
        lines
    }
    pub fn finish(&mut self) -> Option<Vec<u8>> {
        if self.dropping {
            self.dropping = false;
            return None;
        }
        if self.buf.last() == Some(&b'\r') {
            self.buf.pop();
        }
        (!self.buf.is_empty()).then(|| std::mem::take(&mut self.buf))
    }
}

pub fn event_object(line: &[u8]) -> Option<serde_json::Value> {
    let value: serde_json::Value = serde_json::from_str(std::str::from_utf8(line).ok()?).ok()?;
    value.is_object().then_some(value)
}
pub fn unknown_event(ts_ms: u64) -> Signal {
    Signal {
        kind: Kind::Request,
        ts_ms,
        tool: None,
    }
}
pub trait EventParser {
    fn feed(&mut self, chunk: &[u8], now_ms: u64) -> Vec<Signal>;
    fn health(&self) -> &ChannelHealth;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LineKind {
    Known,
    Unknown,
    NotJson,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BlindReason {
    NoParser,
    VersionDrift,
    NoParseWithinF,
    BadRatio,
}

/// Counters and policy only: no event text or clock access.
#[derive(Debug)]
pub struct ChannelHealth {
    t0_ms: u64,
    first_activity_s: Option<u64>,
    lines: u64,
    parsed: u64,
    unknown: u64,
    reason: Option<BlindReason>,
}
impl ChannelHealth {
    pub fn new(t0_ms: u64, first_activity_s: Option<u64>) -> Self {
        Self {
            t0_ms,
            first_activity_s,
            lines: 0,
            parsed: 0,
            unknown: 0,
            reason: None,
        }
    }
    pub fn no_parser(t0_ms: u64) -> Self {
        Self {
            reason: Some(BlindReason::NoParser),
            ..Self::new(t0_ms, None)
        }
    }
    pub fn pin(&mut self, pinned: &str, seen: &str) {
        if pinned != seen {
            self.reason.get_or_insert(BlindReason::VersionDrift);
        }
    }
    pub fn line(&mut self, kind: LineKind, now_ms: u64) {
        // Observe an expired F before a late good line can hide it.
        self.blind(now_ms);
        self.lines += 1;
        if kind != LineKind::NotJson {
            self.parsed += 1;
        }
        if kind == LineKind::Unknown {
            self.unknown += 1;
        }
        self.blind(now_ms);
    }
    pub fn blind(&mut self, now_ms: u64) -> Option<BlindReason> {
        if self.reason.is_none() {
            if self.bad() > 0
                && self.parsed == 0
                && self.first_activity_s.is_some_and(|f| {
                    u128::from(now_ms.saturating_sub(self.t0_ms)) >= u128::from(f) * 1000
                })
            {
                self.reason = Some(BlindReason::NoParseWithinF);
            } else if self.lines >= 20 && self.bad() > self.lines / 2 {
                self.reason = Some(BlindReason::BadRatio);
            }
        }
        self.reason
    }
    pub fn lines(&self) -> u64 {
        self.lines
    }
    pub fn parsed(&self) -> u64 {
        self.parsed
    }
    pub fn bad(&self) -> u64 {
        self.lines - self.parsed
    }
    pub fn unknown(&self) -> u64 {
        self.unknown
    }
}
