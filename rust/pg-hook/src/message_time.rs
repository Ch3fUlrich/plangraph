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

/// The most a stated time may differ from the UTC clock.
const TOLERANCE: Duration = Duration::minutes(2);

/// The first stamp that is not a real, current time: a placeholder, a bare clock time more than
/// two minutes from `now` in either direction, or an instant more than two minutes in the future.
fn invalid_time(text: &str, now: DateTime<Utc>) -> Option<(&str, &'static str)> {
    for found in TIMESTAMP.find_iter(text) {
        let token = found.as_str();
        let normalized = token.to_ascii_uppercase();
        if normalized.contains('X') {
            return Some((token, "has placeholder digits; state the real time"));
        }
        if normalized.contains('-') || normalized.contains('T') {
            let mut iso = normalized.replace(' ', "T");
            if iso.as_bytes().get(16) != Some(&b':') {
                iso.insert_str(16, ":00");
            }
            match DateTime::parse_from_rfc3339(&iso).map(|value| value.to_utc()) {
                Ok(instant) if instant > now + TOLERANCE => {
                    return Some((token, "is later than the UTC clock plus two minutes"))
                }
                Err(_) => return Some((token, "is not a valid timestamp")),
                Ok(_) => {}
            }
        } else {
            match clock_distance(&normalized, now) {
                Some(distance) if distance > TOLERANCE => {
                    return Some((token, "is not within two minutes of the UTC clock"))
                }
                None => return Some((token, "is not a valid timestamp")),
                Some(_) => {}
            }
        }
    }
    None
}

/// Distance between a bare `HH:MMZ` or `HH:MM:SSZ` time and `now`, around midnight too.
fn clock_distance(token: &str, now: DateTime<Utc>) -> Option<Duration> {
    let text = token.strip_suffix('Z')?;
    let format = if text.len() == 5 { "%H:%M" } else { "%H:%M:%S" };
    let time = NaiveTime::parse_from_str(text, format).ok()?;
    let stated = now.date_naive().and_time(time).and_utc();
    [-1, 0, 1]
        .into_iter()
        .map(|days| (stated + Duration::days(days) - now).abs())
        .min()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn refused(text: &str, now: &str) -> Option<&'static str> {
        invalid_time(text, now.parse().unwrap()).map(|(_, reason)| reason)
    }

    #[test]
    fn clock_times_must_be_within_two_minutes_either_way() {
        let now = "2026-11-22T04:02:00Z";
        for pass in ["04:00Z", "04:04Z", "04:04:00Z", "04:00:00Z", "04:02Z"] {
            assert_eq!(refused(pass, now), None, "{pass}");
        }
        for fail in [
            "03:59Z",
            "04:04:01Z",
            "03:59:59Z",
            "04:12Z",
            "03:52Z",
            "16:02Z",
        ] {
            assert!(refused(fail, now).is_some(), "{fail}");
        }
    }

    #[test]
    fn clock_distance_wraps_at_midnight() {
        assert_eq!(refused("23:59Z", "2026-11-22T00:01:00Z"), None);
        assert_eq!(refused("00:01Z", "2026-11-22T23:59:00Z"), None);
        assert!(refused("23:57Z", "2026-11-22T00:01:00Z").is_some());
    }

    #[test]
    fn placeholders_are_refused_with_or_without_about() {
        let now = "2026-11-24T05:27:25Z";
        for fail in [
            "05:4xZ",
            "about 05:4xZ",
            "about 05:2xZ",
            "05:2xZ",
            "0x:27Z",
            "05:2x:25Z",
        ] {
            let reason = refused(fail, now).unwrap_or_else(|| panic!("{fail}"));
            assert!(reason.contains("placeholder"), "{fail}: {reason}");
        }
    }

    #[test]
    fn iso_instants_follow_their_offset_and_may_not_be_in_the_future() {
        let now = "2026-11-22T04:02:00Z";
        for pass in [
            "2026-11-22T05:03:00+01:00",
            "2026-11-22T04:04Z",
            "2026-11-22T04:04:00Z",
            "2026-11-20T04:04:00Z",
        ] {
            assert_eq!(refused(pass, now), None, "{pass}");
        }
        for fail in [
            "2026-11-22T04:04:01Z",
            "2026-11-22T05:04:01+01:00",
            "2026-11-22T03:05:01-01:00",
            "2026-11-22T05:30Z",
        ] {
            assert!(refused(fail, now).is_some(), "{fail}");
        }
    }

    #[test]
    fn the_guessed_stamps_of_2026_11_24_are_refused_and_the_real_clock_passes() {
        // Messages said 05:4xZ and 05:5xZ while the clock read 05:27:25Z.
        let now = "2026-11-24T05:27:25Z";
        for fail in ["05:4xZ", "05:5xZ", "05:37:26Z", "04:47Z"] {
            assert!(refused(fail, now).is_some(), "{fail}");
        }
        for pass in [
            "05:27Z",
            "05:28:25Z",
            "05:25:30Z",
            "2026-11-24T04:47:51+01:00",
        ] {
            assert_eq!(refused(pass, now), None, "{pass}");
        }
    }
}
