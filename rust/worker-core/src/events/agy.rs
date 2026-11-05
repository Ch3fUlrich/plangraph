//! Receive-time signals; only the previous step key survives a parsed line.
use super::{event_object, unknown_event, ChannelHealth, EventParser, Framer, Line, LineKind};
use crate::activity::{Kind, Phase, Signal, ToolClass};
use serde_json::Value;

#[cfg(test)]
mod tests;

pub const RECORDED: &[&str] = &["1.4.12", "1.5.1"];

pub struct AgyParser {
    framer: Framer,
    health: ChannelHealth,
    previous: Option<(Option<Value>, String, Option<Value>)>,
}
impl AgyParser {
    pub fn new(t0_ms: u64, first_activity_s: Option<u64>, version: &str) -> Self {
        let mut health = ChannelHealth::new(t0_ms, first_activity_s);
        let pinned = if RECORDED.contains(&version) {
            version
        } else {
            RECORDED.last().copied().unwrap_or("")
        };
        health.pin(pinned, version);
        Self {
            framer: Framer::new(),
            health,
            previous: None,
        }
    }
    pub fn finish_input(&mut self, now_ms: u64) -> Vec<Signal> {
        self.framer
            .finish()
            .and_then(|v| self.line(Line::Bytes(v), now_ms))
            .into_iter()
            .collect()
    }
    fn unknown(&mut self, now_ms: u64) -> Option<Signal> {
        self.health.line(LineKind::Unknown, now_ms);
        Some(unknown_event(now_ms))
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
        let mut tool = None;
        let kind = match v.get("event").and_then(Value::as_str) {
            Some("init") => Some(Kind::Request),
            Some("step_update") => {
                let step = v.get("step_update").filter(|s| s.is_object()).unwrap_or(&v);
                let Some(name) = step.get("step_type").and_then(Value::as_str) else {
                    return self.unknown(now_ms);
                };
                let key = (
                    step.get("step_index").cloned(),
                    name.to_owned(),
                    step.get("state").cloned(),
                );
                let delta = step
                    .get("text_delta")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty());
                if self.previous.as_ref() == Some(&key) && !delta {
                    None
                } else {
                    self.previous = Some(key);
                    Some(match name {
                        "user_input" => Kind::Request,
                        "agent_response" => Kind::Content,
                        _ => {
                            tool = Some((ToolClass::Other, Phase::Done));
                            Kind::Tool
                        }
                    })
                }
            }
            Some("result") => {
                let result = v.get("result").filter(|r| r.is_object()).unwrap_or(&v);
                let failed = result
                    .get("status")
                    .and_then(Value::as_str)
                    .is_some_and(|s| s != "SUCCESS")
                    || result
                        .get("error")
                        .is_some_and(|e| !e.is_null() && e.as_str() != Some(""));
                Some(if failed { Kind::Error } else { Kind::Request })
            }
            _ => return self.unknown(now_ms),
        };
        self.health.line(LineKind::Known, now_ms);
        kind.map(|kind| Signal {
            kind,
            ts_ms: now_ms,
            tool,
        })
    }
}
impl EventParser for AgyParser {
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
