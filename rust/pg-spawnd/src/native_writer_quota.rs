//! Codex usage-limit classification for native writer turns: a turn that hit the limit is
//! a quota stop, never a failure worth relaunching on the same exhausted account.
use serde_json::Value;

/// Prefix of the writer-turn error that carries a usage-limit stop.
pub(crate) const PREFIX: &str = "quota_limited:";
/// Run exit code of a writer that stopped on a usage limit.
pub(crate) const EXIT: i32 = 11;
const PHRASE: &str = "hit your usage limit";

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Limit {
    /// The provider message, as reported.
    pub line: String,
    /// The reset time as the message spells it.
    pub reset: Option<String>,
    /// The reset time as seconds since the epoch, read in the host's local zone.
    pub reset_epoch: Option<i64>,
}

impl Limit {
    /// The reset time for the status line: UTC when it parsed, the message's text otherwise.
    pub fn label(&self) -> String {
        match (self.reset_epoch, &self.reset) {
            (Some(epoch), _) => chrono::DateTime::from_timestamp(epoch, 0)
                .map(|at| at.format("%Y-%m-%dT%H:%MZ").to_string())
                .unwrap_or_else(|| epoch.to_string()),
            (None, Some(text)) => text.clone(),
            (None, None) => "unknown".into(),
        }
    }
    pub fn error(&self) -> String {
        format!("{PREFIX} {}", self.line)
    }
}

/// The usage-limit stop in `text` (a log line or an event message), if it is one.
pub(crate) fn classify(text: &str) -> Option<Limit> {
    let folded = text.replace('\u{2019}', "'");
    if !folded.to_ascii_lowercase().contains(PHRASE) {
        return None;
    }
    let reset = folded
        .find("try again at ")
        .map(|at| &folded[at + "try again at ".len()..])
        .map(|rest| {
            rest.split(['.', '"'])
                .next()
                .unwrap_or("")
                .trim()
                .to_owned()
        })
        .filter(|text| !text.is_empty());
    let reset_epoch = reset.as_deref().and_then(epoch);
    Some(Limit {
        line: folded.trim().chars().take(300).collect(),
        reset,
        reset_epoch,
    })
}

/// One log line of a failed turn: a plain error line, or an `error` / `turn.failed` event.
/// Agent messages and command output never count, so a writer quoting the text is not a stop.
pub(crate) fn from_log_line(line: &str) -> Option<Limit> {
    match serde_json::from_str::<Value>(line) {
        Err(_) => classify(line),
        Ok(event) => match event["type"].as_str() {
            Some("error") => event["message"].as_str().and_then(classify),
            Some("turn.failed") => event["error"]["message"].as_str().and_then(classify),
            _ => None,
        },
    }
}

/// The stop a writer-turn error carries.
pub(crate) fn from_error(error: &str) -> Option<Limit> {
    error.strip_prefix(PREFIX).and_then(classify)
}

/// "Nov 27th, 2026 8:24 PM" in the host's local zone.
fn epoch(text: &str) -> Option<i64> {
    let words: Vec<String> = text
        .split_whitespace()
        .map(|word| {
            let day = word.trim_end_matches(',');
            let digits = ["st", "nd", "rd", "th"]
                .iter()
                .find_map(|suffix| day.strip_suffix(suffix))
                .filter(|digits| !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()));
            digits.map_or_else(|| word.to_owned(), |digits| format!("{digits},"))
        })
        .collect();
    let naive =
        chrono::NaiveDateTime::parse_from_str(&words.join(" "), "%b %d, %Y %I:%M %p").ok()?;
    naive
        .and_local_timezone(chrono::Local)
        .earliest()
        .map(|at| at.timestamp())
}

#[cfg(test)]
mod tests {
    use super::*;

    const MESSAGE: &str =
        "You've hit your usage limit. Upgrade to Pro, or try again at Nov 27th, 2026 8:24 PM.";

    #[test]
    fn usage_limit_message_gives_the_reset_time() {
        let limit = classify(MESSAGE).unwrap();
        assert_eq!(limit.reset.as_deref(), Some("Nov 27th, 2026 8:24 PM"));
        let local = chrono::NaiveDate::from_ymd_opt(2026, 11, 27)
            .unwrap()
            .and_hms_opt(20, 24, 0)
            .unwrap()
            .and_local_timezone(chrono::Local)
            .earliest()
            .unwrap();
        assert_eq!(limit.reset_epoch, Some(local.timestamp()));
        assert!(limit.label().starts_with("2026-11-2"));
    }

    #[test]
    fn a_limit_without_a_reset_time_names_it_unknown() {
        let limit = classify("ERROR: You\u{2019}ve hit your usage limit.").unwrap();
        assert_eq!((&limit.reset, limit.reset_epoch), (&None, None));
        assert_eq!(limit.label(), "unknown");
    }

    #[test]
    fn only_error_lines_and_events_are_a_stop() {
        let error = serde_json::json!({"type":"error","message":MESSAGE}).to_string();
        let failed = serde_json::json!({"type":"turn.failed","error":{"message":MESSAGE}});
        let quoted = serde_json::json!({"type":"item.completed",
            "item":{"type":"agent_message","text":MESSAGE}});
        assert!(from_log_line(&error).is_some());
        assert!(from_log_line(&failed.to_string()).is_some());
        assert!(from_log_line(MESSAGE).is_some());
        assert!(from_log_line(&quoted.to_string()).is_none());
        assert!(from_log_line("{\"type\":\"turn.completed\"}").is_none());
        assert!(from_log_line("compiling usage limits").is_none());
    }

    #[test]
    fn the_turn_error_round_trips() {
        let limit = classify(MESSAGE).unwrap();
        assert_eq!(from_error(&limit.error()), Some(limit));
        assert_eq!(from_error("codex failed: exit status: 1"), None);
    }
}
