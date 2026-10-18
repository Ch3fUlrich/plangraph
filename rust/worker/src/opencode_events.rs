//! Classify an `opencode run --format json` event stream.
//!
//! The CLI prints one JSON object per line, but the same stream may carry
//! plain stderr text, so every line is parsed on its own and a line that is
//! not a recognised event is skipped. Everything here is pure: a stream in, a
//! summary out. No I/O and no panics — the worker uses the summary to tell a
//! provider limit from a crash, and a run that did nothing from one that did
//! the work.

use serde::Deserialize;

/// An error the provider reported in the stream.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderError {
    pub kind: ProviderErrorKind,
    /// HTTP status the provider attached, when it sent one.
    pub status: Option<u16>,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderErrorKind {
    /// Out of quota or rate-limited: retry later, or reroute to another leg.
    RateLimit,
    /// Anything else (transport failure, unknown provider error, ...).
    Other,
}

/// What one run's event stream contained.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EventSummary {
    pub provider_error: Option<ProviderError>,
    /// A `text` event carried non-whitespace text.
    pub saw_text: bool,
    pub tool_calls: u32,
    /// Tool calls refused because the target is outside the workspace.
    pub permission_denied: u32,
    pub tokens_in: Option<u64>,
    pub tokens_out: Option<u64>,
}

impl EventSummary {
    /// No provider error, no text and no tool calls: the run did nothing.
    pub fn is_empty_run(&self) -> bool {
        self.provider_error.is_none() && !self.saw_text && self.tool_calls == 0
    }
}

/// One top-level stream line. Unknown fields are ignored by serde.
#[derive(Debug, Default, Deserialize)]
struct Event {
    #[serde(rename = "type", default)]
    kind: String,
    #[serde(default)]
    part: Part,
    #[serde(default)]
    error: Option<ErrorPayload>,
}

#[derive(Debug, Default, Deserialize)]
struct Part {
    #[serde(default)]
    text: String,
    #[serde(default)]
    tokens: Tokens,
    #[serde(default)]
    state: ToolState,
}

/// Token counters of a `step_finish`. Every field defaults so a partial
/// `tokens` object still yields its input/output instead of dropping the line.
///
/// Only `input` and `output` are counted: a cache read is a hit, not new input,
/// and reasoning tokens are not billed as output. The rest is parsed only to
/// pin the wire shape.
#[allow(dead_code)]
#[derive(Debug, Default, Deserialize)]
struct Tokens {
    #[serde(default)]
    input: u64,
    #[serde(default)]
    output: u64,
    #[serde(default)]
    reasoning: u64,
    #[serde(default)]
    cache: CacheTokens,
}

#[allow(dead_code)]
#[derive(Debug, Default, Deserialize)]
struct CacheTokens {
    #[serde(default)]
    read: u64,
    #[serde(default)]
    write: u64,
}

#[derive(Debug, Default, Deserialize)]
struct ToolState {
    #[serde(default)]
    status: String,
    #[serde(default)]
    error: Option<String>,
}

impl ToolState {
    /// A tool call that failed *because permission was refused*. A plain tool
    /// error (compile failure, bad input) is not a confinement event.
    fn is_permission_denied(&self) -> bool {
        self.status == "error"
            && self
                .error
                .as_deref()
                .is_some_and(|m| m.to_ascii_lowercase().contains("denied"))
    }
}

#[derive(Debug, Default, Deserialize)]
struct ErrorPayload {
    #[serde(rename = "type", default)]
    kind: String,
    #[serde(default)]
    message: String,
    #[serde(default)]
    status: Option<u16>,
}

impl ErrorPayload {
    fn provider_kind(&self) -> ProviderErrorKind {
        let kind = self.kind.to_ascii_lowercase();
        if self.status == Some(429) || kind.contains("quota") || kind.contains("rate") {
            ProviderErrorKind::RateLimit
        } else {
            ProviderErrorKind::Other
        }
    }
}

/// Summarise every recognised event in `stream`, skipping lines that are not
/// JSON (stderr text) and objects this module does not model.
pub fn summarize(stream: &str) -> EventSummary {
    let mut out = EventSummary::default();
    let mut steps = 0u32;
    let mut tokens_in = 0u64;
    let mut tokens_out = 0u64;
    for line in stream.lines() {
        let Ok(ev) = serde_json::from_str::<Event>(line) else {
            continue;
        };
        match ev.kind.as_str() {
            "text" => {
                if !ev.part.text.trim().is_empty() {
                    out.saw_text = true;
                }
            }
            "tool_use" => {
                out.tool_calls = out.tool_calls.saturating_add(1);
                if ev.part.state.is_permission_denied() {
                    out.permission_denied = out.permission_denied.saturating_add(1);
                }
            }
            "step_finish" => {
                steps = steps.saturating_add(1);
                // `cache.read` is a cache hit, not new input: never add it.
                tokens_in = tokens_in.saturating_add(ev.part.tokens.input);
                tokens_out = tokens_out.saturating_add(ev.part.tokens.output);
            }
            // The first error is the cause; anything after it is fallout, so the
            // guard keeps later "error" events falling through to `_`.
            "error" if out.provider_error.is_none() => {
                let err = ev.error.unwrap_or_default();
                out.provider_error = Some(ProviderError {
                    kind: err.provider_kind(),
                    status: err.status,
                    message: err.message,
                });
            }
            _ => {}
        }
    }
    if steps > 0 {
        out.tokens_in = Some(tokens_in);
        out.tokens_out = Some(tokens_out);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const RATE_LIMITED: [&str; 2] = [
        include_str!("../tests/fixtures/opencode/real-429-big-pickle.jsonl"),
        include_str!("../tests/fixtures/opencode/real-429-deepseek-v4.1-flash-free.jsonl"),
    ];
    const TRANSPORT: &str =
        include_str!("../tests/fixtures/opencode/real-provider-transport-error.jsonl");
    const OK_TEXT: &str = include_str!("../tests/fixtures/opencode/real-ok-text.jsonl");
    const OK_TOOL_TOKENS: &str =
        include_str!("../tests/fixtures/opencode/real-ok-tool-tokens.jsonl");
    const DENIED: &str =
        include_str!("../tests/fixtures/opencode/real-permission-denied-external.jsonl");

    #[test]
    fn quota_errors_are_rate_limits() {
        for raw in RATE_LIMITED {
            let s = summarize(raw);
            let err = s.provider_error.as_ref().expect("provider error");
            assert_eq!(err.kind, ProviderErrorKind::RateLimit);
            assert_eq!(err.status, Some(429));
            assert!(err.message.contains("Rate limit"));
            assert!(!s.is_empty_run());
        }
    }

    #[test]
    fn transport_error_is_not_a_limit() {
        let s = summarize(TRANSPORT);
        let err = s.provider_error.as_ref().expect("provider error");
        assert_eq!(err.kind, ProviderErrorKind::Other);
        assert_eq!(err.status, None);
        assert!(err.message.contains("Transport"));
        assert!(!s.is_empty_run());
    }

    #[test]
    fn text_only_run_has_text_and_no_tokens() {
        let s = summarize(OK_TEXT);
        assert!(s.saw_text);
        assert!(s.provider_error.is_none());
        assert_eq!(s.tool_calls, 0);
        assert_eq!(s.permission_denied, 0);
        assert_eq!(s.tokens_in, None);
        assert_eq!(s.tokens_out, None);
        assert!(!s.is_empty_run());
    }

    #[test]
    fn tool_run_counts_calls_and_tokens() {
        let s = summarize(OK_TOOL_TOKENS);
        assert_eq!(s.tokens_in, Some(13318));
        assert_eq!(s.tokens_out, Some(50));
        assert!(s.tool_calls >= 1);
        assert!(s.saw_text);
        assert_eq!(s.permission_denied, 0);
    }

    #[test]
    fn denied_external_write_is_counted() {
        let s = summarize(DENIED);
        assert!(s.permission_denied >= 1);
        assert_eq!(s.tool_calls, 2);
    }

    #[test]
    fn nothing_happened_is_an_empty_run() {
        assert!(summarize("").is_empty_run());
        let lone_step = r#"{"type":"step_start","part":{"type":"step-start"}}"#;
        assert!(summarize(lone_step).is_empty_run());
        // Whitespace-only text is not output either.
        let blank = r#"{"type":"text","part":{"text":"  \n\t "}}"#;
        assert!(summarize(blank).is_empty_run());
    }

    #[test]
    fn garbage_lines_are_skipped() {
        let raw = r#"
            stderr: warming up
            not json at all
            {"type":
            {"type":"text","part":{"text":"hi"}}
            [1,2,3]
            {"type":"no_such_event"}
        "#;
        let s = summarize(raw);
        assert!(s.saw_text);
        assert!(s.provider_error.is_none());
        assert_eq!(s.tool_calls, 0);
        assert_eq!(summarize("boom\n\n{").provider_error, None);
    }

    #[test]
    fn step_finish_without_reasoning_or_cache_still_counts() {
        let raw = r#"{"type":"step_finish","part":{"tokens":{"input":7,"output":3}}}"#;
        let s = summarize(raw);
        assert_eq!(s.tokens_in, Some(7));
        assert_eq!(s.tokens_out, Some(3));
    }

    #[test]
    fn tokens_accumulate_across_steps_and_cache_reads_do_not_count() {
        let raw = "{\"type\":\"step_finish\",\"part\":{\"tokens\":\
                   {\"input\":10,\"output\":2,\"cache\":{\"read\":900,\"write\":5}}}}\n\
                   {\"type\":\"step_finish\",\"part\":{\"tokens\":{\"input\":1,\"output\":4}}}";
        let s = summarize(raw);
        assert_eq!(s.tokens_in, Some(11));
        assert_eq!(s.tokens_out, Some(6));
    }

    #[test]
    fn a_plain_tool_error_is_not_a_denial() {
        let raw =
            r#"{"type":"tool_use","part":{"state":{"status":"error","error":"build failed"}}}"#;
        let s = summarize(raw);
        assert_eq!(s.tool_calls, 1);
        assert_eq!(s.permission_denied, 0);
    }
}
