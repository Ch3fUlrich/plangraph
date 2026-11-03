//! Receive-time activity only; raw error classification belongs to the caller.
use super::{event_object, unknown_event, ChannelHealth, EventParser, Framer, Line, LineKind};
use crate::activity::{Kind, Phase, Signal, ToolClass};

#[cfg(test)]
mod capture_tests;
#[cfg(test)]
mod fixtures;
#[cfg(test)]
mod tests;

#[derive(Debug)]
pub struct OpencodeParser {
    framer: Framer,
    health: ChannelHealth,
}
impl OpencodeParser {
    pub fn new(t0_ms: u64, first_activity_s: Option<u64>, pinned: &str, seen: &str) -> Self {
        let mut health = ChannelHealth::new(t0_ms, first_activity_s);
        health.pin(pinned, seen);
        Self {
            framer: Framer::new(),
            health,
        }
    }
    pub fn end(&mut self, now_ms: u64) -> Vec<Signal> {
        self.framer
            .finish()
            .and_then(|v| self.line(Line::Bytes(v), now_ms))
            .into_iter()
            .collect()
    }
    fn line(&mut self, line: Line, now_ms: u64) -> Option<Signal> {
        let value = match line {
            Line::Bytes(bytes) => event_object(&bytes),
            Line::Overlong => None,
        };
        let Some(v) = value else {
            self.health.line(LineKind::NotJson, now_ms);
            return None;
        };
        let part = v.get("part").filter(|p| p.is_object());
        let mut tool = None;
        let kind = match v["type"].as_str() {
            Some("step_start") if part.is_some() => Some(Kind::Request),
            Some("step_finish") if part.is_some() => None,
            Some("text" | "reasoning") if part.is_some() => match v["part"].get("text") {
                None => None,
                Some(t) if t.is_string() => t
                    .as_str()
                    .filter(|s| !s.trim().is_empty())
                    .map(|_| Kind::Content),
                _ => return self.unknown(now_ms),
            },
            Some("tool_use") => {
                let class = match v["part"]["tool"].as_str() {
                    Some("read" | "grep" | "glob" | "list") => ToolClass::Read,
                    Some("bash" | "shell") => ToolClass::Exec,
                    Some("edit" | "write" | "patch") => ToolClass::Edit,
                    _ => ToolClass::Other,
                };
                let class = match v["part"]["state"]["status"].as_str() {
                    Some("completed") => class,
                    Some("error") => ToolClass::Other,
                    Some("running" | "pending") => {
                        self.health.line(LineKind::Known, now_ms);
                        return None;
                    }
                    _ => return self.unknown(now_ms),
                };
                tool = Some((class, Phase::Done));
                Some(Kind::Tool)
            }
            Some("error") if v["error"].is_object() => Some(Kind::Error),
            _ => return self.unknown(now_ms),
        };
        self.health.line(LineKind::Known, now_ms);
        kind.map(|kind| Signal {
            kind,
            ts_ms: now_ms,
            tool,
        })
    }
    fn unknown(&mut self, now_ms: u64) -> Option<Signal> {
        self.health.line(LineKind::Unknown, now_ms);
        Some(unknown_event(now_ms))
    }
}
impl EventParser for OpencodeParser {
    fn feed(&mut self, chunk: &[u8], now_ms: u64) -> Vec<Signal> {
        self.framer
            .push(chunk)
            .into_iter()
            .filter_map(|line| self.line(line, now_ms))
            .collect()
    }
    fn health(&self) -> &ChannelHealth {
        &self.health
    }
}
