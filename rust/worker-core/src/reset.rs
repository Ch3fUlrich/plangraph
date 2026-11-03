//! One-shot RAW reset capture; no clock, I/O, or class defaults.
//! `reset_assumed` belongs to the fold, not capture. Open issue: RL18a owns
//! live `rateLimitedUntil` parsing and must not apply the one-shot clamp.

use regex::Regex;

#[cfg(test)]
mod tests;

pub const CLAMP_S: f64 = 86_400.0;

#[derive(Debug, PartialEq)]
pub struct Reset {
    pub reset_at: f64,
    pub clamped: bool,
}

/// Clamp using the row's receive time, never the fold's current time.
pub fn clamp_until(reset_at: f64, ts: f64) -> (f64, bool) {
    (reset_at.min(ts + CLAMP_S), reset_at > ts + CLAMP_S)
}

fn number(value: &str) -> Option<f64> {
    value
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|n| n.is_finite() && *n >= 0.0)
}

fn absolute(value: f64, date: Option<f64>, ts: f64) -> Option<f64> {
    let date = date.filter(|d| d.is_finite())?;
    Some(ts + (value - date).max(0.0))
}

fn duration(value: &str, compact: bool) -> Option<f64> {
    let pair = Regex::new(r"(?i)^\s*(\d+(?:\.\d+)?)\s*([a-z]+)").unwrap();
    // Compact headers allow adjacent pairs, so their unit ends at the next digit.
    let compact_pair = Regex::new(r"(?i)^(\d+(?:\.\d+)?)([a-z]+)").unwrap();
    let mut rest = value;
    let mut total = 0.0;
    let mut seen = false;
    loop {
        let re = if compact { &compact_pair } else { &pair };
        let Some(c) = re.captures(rest) else {
            return (seen && (!compact || rest.is_empty())).then_some(total);
        };
        let factor = match c[2].to_ascii_lowercase().as_str() {
            "ms" | "msec" | "msecs" | "millisecond" | "milliseconds" => 0.001,
            "s" | "sec" | "secs" | "second" | "seconds" => 1.0,
            "m" | "min" | "mins" | "minute" | "minutes" => 60.0,
            "h" | "hr" | "hrs" | "hour" | "hours" => 3600.0,
            "d" | "day" | "days" => CLAMP_S,
            _ => return None,
        };
        total += number(&c[1])? * factor;
        if !total.is_finite() {
            return None;
        }
        rest = &rest[c.get(0)?.end()..];
        seen = true;
    }
}

/// Capture the first usable source, preserving the provider's raw reset time.
pub fn capture(
    headers: &[(&str, &str)],
    body: &str,
    response_date: Option<f64>,
    ts: f64,
) -> Option<Reset> {
    let finish = |reset_at: f64| Reset {
        reset_at,
        clamped: reset_at > ts + CLAMP_S,
    };
    for name in [
        "retry-after",
        "x-ratelimit-reset",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    ] {
        let Some((_, value)) = headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
        else {
            continue;
        };
        let value = value.trim();
        let at = if let Some(n) = number(value) {
            if name == "retry-after" || n < 1e9 {
                Some(ts + n)
            } else {
                absolute(n, response_date, ts)
            }
        } else {
            let relative = (name != "retry-after")
                .then(|| duration(value, true))
                .flatten();
            relative.map(|n| ts + n).or_else(|| {
                // Retry-After accepts HTTP-date only, not RFC 3339.
                if name == "retry-after" && !value.contains(',') {
                    return None;
                }
                absolute(http_date(value)?, response_date, ts)
            })
        };
        if let Some(at) = at {
            return Some(finish(at));
        }
    }
    for field in ["resetsAt", "retryAfterMs"] {
        let re = Regex::new(&format!(
            r#""{field}"\s*:\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?:\s*[,}}\]]|\s*$)"#
        ))
        .unwrap();
        if let Some(c) = re.captures(body) {
            if let Some(n) = number(&c[1]) {
                let at = if field == "retryAfterMs" {
                    Some(ts + n / 1000.0)
                } else {
                    absolute(if n >= 1e12 { n / 1000.0 } else { n }, response_date, ts)
                };
                if let Some(at) = at {
                    return Some(finish(at));
                }
            }
        }
    }
    let prefix = Regex::new(r"(?i)resets?\s+(?:in|after)\s+").unwrap();
    let start = prefix.find(body)?.end();
    duration(&body[start..], false).map(|n| finish(ts + n))
}

/// Parse IMF-fixdate or RFC 3339 to epoch seconds; weekday is not cross-checked.
pub fn http_date(value: &str) -> Option<f64> {
    let imf =
        Regex::new(r"^[A-Za-z]{3}, (\d{2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$")
            .unwrap();
    let iso = Regex::new(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)(Z|[+-]\d{2}:\d{2})$",
    )
    .unwrap();
    let (year, month, day, hour, minute, second, offset) = if let Some(c) = imf.captures(value) {
        let month = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]
        .iter()
        .position(|m| *m == &c[2])?
            + 1;
        (
            c[3].parse::<i64>().ok()?,
            month as i64,
            c[1].parse::<i64>().ok()?,
            c[4].parse::<i64>().ok()?,
            c[5].parse::<i64>().ok()?,
            c[6].parse::<f64>().ok()?,
            0,
        )
    } else {
        let c = iso.captures(value)?;
        let zone = &c[7];
        let offset = if zone == "Z" {
            0
        } else {
            let h = zone[1..3].parse::<i64>().ok()?;
            let m = zone[4..6].parse::<i64>().ok()?;
            if h > 23 || m > 59 {
                return None;
            }
            (h * 3600 + m * 60) * if zone.starts_with('-') { -1 } else { 1 }
        };
        (
            c[1].parse::<i64>().ok()?,
            c[2].parse::<i64>().ok()?,
            c[3].parse::<i64>().ok()?,
            c[4].parse::<i64>().ok()?,
            c[5].parse::<i64>().ok()?,
            c[6].parse::<f64>().ok()?,
            offset,
        )
    };
    if !(1..=12).contains(&month) || hour > 23 || minute > 59 || second >= 60.0 {
        return None;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if day < 1 || day > days[(month - 1) as usize] {
        return None;
    }
    // Gregorian civil date to days since 1970-01-01, including pre-epoch dates.
    let y = year - i64::from(month <= 2);
    let era = y.div_euclid(400);
    let yo = y - era * 400;
    let mp = month + if month > 2 { -3 } else { 9 };
    let doy = (153 * mp + 2) / 5 + day - 1;
    let epoch_days = era * 146097 + yo * 365 + yo / 4 - yo / 100 + doy - 719468;
    Some((epoch_days * 86400 + hour * 3600 + minute * 60 - offset) as f64 + second)
}
