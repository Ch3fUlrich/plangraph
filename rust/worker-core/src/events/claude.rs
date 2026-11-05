//! Claude-compatible streams: retain activity metadata, not message payloads.
use super::{event_object, unknown_event, ChannelHealth, EventParser, Framer, Line, LineKind};
use crate::activity::{Kind, Phase, Signal, ToolClass};
use std::collections::HashMap;

#[cfg(test)]
mod tests;

pub const RECORDED_CLAUDE: &[&str] = &["2.1.318"];
pub const RECORDED_QODERCLI: &[&str] = &["1.1.69"];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flavor {
    Claude,
    Qodercli,
}
pub struct ClaudeParser {
    flavor: Flavor,
    framer: Framer,
    health: ChannelHealth,
    tools: HashMap<String, ToolClass>,
}
impl ClaudeParser {
    pub fn new(flavor: Flavor, t0_ms: u64, first_activity_s: Option<u64>, version: &str) -> Self {
        let versions = match flavor {
            Flavor::Claude => RECORDED_CLAUDE,
            Flavor::Qodercli => RECORDED_QODERCLI,
        };
        let pinned = if versions.contains(&version) {
            version
        } else {
            versions.last().copied().unwrap_or("unrecorded")
        };
        let mut health = ChannelHealth::new(t0_ms, first_activity_s);
        health.pin(pinned, version);
        Self {
            flavor,
            framer: Framer::new(),
            health,
            tools: HashMap::new(),
        }
    }
    pub fn finish_input(&mut self, now_ms: u64) -> Vec<Signal> {
        self.framer
            .finish()
            .map_or_else(Vec::new, |b| self.line(Line::Bytes(b), now_ms))
    }
    fn line(&mut self, line: Line, now_ms: u64) -> Vec<Signal> {
        let value = match line {
            Line::Bytes(b) => event_object(&b),
            Line::Overlong => None,
        };
        let Some(v) = value else {
            self.health.line(LineKind::NotJson, now_ms);
            return Vec::new();
        };
        let mut out = Vec::new();
        let mut emit = |kind, tool| {
            out.push(Signal {
                kind,
                ts_ms: now_ms,
                tool,
            })
        };
        let known = match v["type"].as_str() {
            Some("system") => {
                if (v["subtype"] == "status" && v["status"] == "requesting")
                    || (v["subtype"] == "init" && self.flavor == Flavor::Qodercli)
                {
                    emit(Kind::Request, None);
                }
                true
            }
            Some("stream_event") if v["event"].is_object() => {
                match v["event"]["type"].as_str() {
                    Some("message_start") => emit(Kind::Request, None),
                    Some("content_block_delta") => emit(Kind::Content, None),
                    _ => {}
                }
                true
            }
            Some("assistant" | "user") => {
                let assistant = v["type"] == "assistant";
                let content = &v["message"]["content"];
                if assistant && content.is_string() {
                    emit(Kind::Content, None);
                }
                for block in content.as_array().into_iter().flatten() {
                    match (assistant, block["type"].as_str()) {
                        (true, Some("text" | "thinking")) => emit(Kind::Content, None),
                        (true, Some("tool_use")) => {
                            let class = tool_class(block["name"].as_str());
                            if self.tools.len() < 256 {
                                if let Some(id) = block["id"].as_str() {
                                    self.tools.insert(id.to_owned(), class);
                                }
                            }
                            emit(Kind::Tool, Some((class, Phase::Start)));
                        }
                        (false, Some("tool_result")) => {
                            let class = block["tool_use_id"]
                                .as_str()
                                .and_then(|id| self.tools.remove(id))
                                .unwrap_or(ToolClass::Other);
                            let class = if block["is_error"] == true {
                                ToolClass::Other
                            } else {
                                class
                            };
                            emit(Kind::Tool, Some((class, Phase::End)));
                        }
                        _ => {}
                    }
                }
                true
            }
            Some("rate_limit_event") => {
                emit(Kind::Request, None);
                true
            }
            Some("result") => {
                let kind = if v["safety_stops"].as_u64().is_some_and(|n| n > 0) {
                    Kind::Error
                } else {
                    Kind::Request
                };
                emit(kind, None);
                true
            }
            _ => false,
        };
        self.health.line(
            if known {
                LineKind::Known
            } else {
                LineKind::Unknown
            },
            now_ms,
        );
        if !known {
            out.push(unknown_event(now_ms));
        }
        out
    }
}
fn tool_class(name: Option<&str>) -> ToolClass {
    match name {
        Some("Read" | "Grep" | "Glob" | "LS") => ToolClass::Read,
        Some("Bash") => ToolClass::Exec,
        Some("Edit" | "MultiEdit" | "Write" | "NotebookEdit") => ToolClass::Edit,
        _ => ToolClass::Other,
    }
}
impl EventParser for ClaudeParser {
    fn feed(&mut self, chunk: &[u8], now_ms: u64) -> Vec<Signal> {
        self.framer
            .push(chunk)
            .into_iter()
            .flat_map(|line| self.line(line, now_ms))
            .collect()
    }
    fn health(&self) -> &ChannelHealth {
        &self.health
    }
}
