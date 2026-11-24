//! Reject future or unmarked approximate timestamps in outgoing agent messages.
use chrono::{DateTime, Duration, NaiveTime, Utc};
use regex::Regex;
use serde_json::Value;
use std::io::{BufRead, Write};
use std::path::Path;
use std::sync::LazyLock;

static TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r"(?i)\b(?:",
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        r"|[0-9x]{2}:[0-9x]{2}(?::[0-9x]{2})?Z",
        r")\b",
    )).expect("timestamp pattern")
});

pub fn hook(input: impl BufRead, diagnostic: &mut impl Write) -> i32 {
    let Ok(payload) = serde_json::from_reader::<_, Value>(input) else {
        return 0;
    };
    if payload["hook_event_name"] != "PreToolUse" {
        return 0;
    }
    let input = &payload["tool_input"];
    let text = match payload["tool_name"].as_str() {
        Some("SendMessage") => input["message"].as_str().map(str::to_owned),
        Some("Bash" | "exec_command") => input["command"]
            .as_str()
            .or_else(|| input["cmd"].as_str())
            .and_then(shell_message),
        _ => None,
    };
    text.as_deref().map_or(0, |text| check(text, diagnostic))
}

pub fn check(text: &str, diagnostic: &mut impl Write) -> i32 {
    let Some((token, reason)) = invalid_time(text, Utc::now()) else {
        return 0;
    };
    let _ = writeln!(diagnostic, "message-time: {token} {reason}");
    2
}

fn shell_message(command: &str) -> Option<String> {
    let args = shlex::split(command)?;
    let [program, action, _, message, rest @ ..] = args.as_slice() else {
        return None;
    };
    // The fleet-msg client checks the final arguments after shell evaluation.
    if Path::new(program).file_name()?.to_str()? == "fleet-msg"
        && action == "send"
        && (rest.is_empty() || rest == ["--json"])
    {
        Some(message.clone())
    } else {
        None
    }
}

fn invalid_time(text: &str, now: DateTime<Utc>) -> Option<(&str, &'static str)> {
    let limit = now + Duration::minutes(1);
    for found in TIMESTAMP.find_iter(text) {
        let token = found.as_str();
        let normalized = token.to_ascii_uppercase();
        let instant = if normalized.contains('-') || normalized.contains('T') {
            let mut iso = normalized.replace(' ', "T");
            if iso.as_bytes().get(16) != Some(&b':') {
                iso.insert_str(16, ":00");
            }
            DateTime::parse_from_rfc3339(&iso)
                .ok()
                .map(|value| value.to_utc())
        } else {
            let prior = text[..found.start()].split_whitespace().next_back();
            if normalized.contains('X')
                && !prior.is_some_and(|word| word.eq_ignore_ascii_case("about"))
            {
                return Some((token, "has placeholder digits and needs 'about'"));
            }
            utc_time(&normalized, now)
        };
        match instant {
            Some(instant) if instant > limit => {
                return Some((token, "is later than the UTC clock plus one minute"))
            }
            None => return Some((token, "is not a valid timestamp")),
            _ => {}
        }
    }
    None
}

fn utc_time(token: &str, now: DateTime<Utc>) -> Option<DateTime<Utc>> {
    let text = token.strip_suffix('Z')?.replace('X', "0");
    let format = if text.len() == 5 { "%H:%M" } else { "%H:%M:%S" };
    let time = NaiveTime::parse_from_str(&text, format).ok()?;
    Some(now.date_naive().and_time(time).and_utc())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tolerance_and_offsets_follow_the_same_clock() {
        let now = "2026-11-22T04:02:00Z".parse().unwrap();
        for pass in [
            "about 04:0xZ",
            "04:03Z",
            "04:03:00Z",
            "2026-11-22T05:03:00+01:00",
            "2026-11-22T04:03Z",
        ] {
            assert_eq!(invalid_time(pass, now), None, "{pass}");
        }
        for fail in [
            "04:03:01Z",
            "04:2xZ",
            "about 04:2xZ",
            "2026-11-22T03:03:01-01:00",
            "2026-11-22T04:04Z",
        ] {
            assert_eq!(
                invalid_time(fail, now).map(|(token, _)| token),
                Some(fail.trim_start_matches("about "))
            );
        }
    }
    #[test]
    fn a_guessed_future_stamp_is_refused_and_the_real_clock_passes() {
        // 2026-11-24: messages said 05:4xZ and 05:5xZ while the clock read 05:27:25Z.
        let now = "2026-11-24T05:27:25Z".parse().unwrap();
        for fail in ["05:4xZ", "about 05:4xZ", "05:5xZ", "05:37:26Z"] {
            assert!(invalid_time(fail, now).is_some(), "{fail}");
        }
        for pass in [
            "about 05:2xZ",
            "05:27Z",
            "05:28:25Z",
            "2026-11-24T04:47:51+01:00",
            "03:47Z",
        ] {
            assert_eq!(invalid_time(pass, now), None, "{pass}");
        }
    }
}
