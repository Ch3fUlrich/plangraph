//! Pure provider evidence scanning; caller integration belongs to RL15.
//! Open issues: RL18a live `rateLimitedUntil`; `autoos_provider_response` envelopes.
use super::{Facts, Marker};
use crate::reset::{self, Reset};
use regex::Regex;
use serde_json::Value;
use std::sync::OnceLock;

#[cfg(test)]
mod classify_tests;
#[cfg(test)]
mod tests;

pub struct Input<'a> {
    pub exit: i32,
    pub stdout: &'a str,
    pub stderr: &'a str,
    pub headers: &'a [(&'a str, &'a str)],
    pub response_date: Option<f64>,
    pub ts: f64,
}

#[derive(Debug)]
pub struct Hit {
    pub marker: Marker,
    pub line: String,
    pub reset: Option<Reset>,
}

impl Facts {
    pub fn with_hit(mut self, hit: Hit) -> Facts {
        self.marker = Some(hit.marker);
        self.evidence = Some(hit.line);
        self.reset = hit.reset;
        self
    }
}

struct Patterns {
    ansi: Regex,
    error: Regex,
    error_json: Regex,
    words: [Regex; 5],
}

fn patterns() -> &'static Patterns {
    static P: OnceLock<Patterns> = OnceLock::new();
    P.get_or_init(|| {
        let re = |s| Regex::new(s).expect("marker pattern compiles");
        Patterns {
            ansi: re(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"),
            error: re(r"(?i)^\s*(?:error:|HTTP \d{3}:)"),
            error_json: re(r#"^\s*(?:\{.*"error"\s*:|"error"\s*:)"#),
            words: [
                re(r"(?i)credits? (?:exhausted|depleted|exceeded|ran out)|out of credits|insufficient (?:credits?|balance|quota)|payment required|unauthori[sz]ed|authentication (?:failed|error|required)|invalid api key"),
                re(r"(?i)model unavailable|not served|unknown model|not_served_on_host"),
                re(r"(?i)service temporarily unavailable|all targets were skipped|circuit breaker is open|\bHTTP 50[234]\b|overloaded|bad gateway"),
                re(r"(?i)quota|usage limit|usagelimit|credit|daily limit"),
                re(r"(?i)rate limit|too many requests|\bHTTP 429\b"),
            ],
        }
    })
}

const ORDER: [Marker; 5] = [
    Marker::Auth,
    Marker::ModelUnavailable,
    Marker::ProviderUnavailable,
    Marker::Quota,
    Marker::RateLimit,
];

fn quota(text: &str) -> bool {
    patterns().words[3].is_match(text)
}

fn event_words(v: &Value) -> bool {
    [&v["message"], &v["rate_limit_info"]["message"]]
        .iter()
        .filter_map(|value| value.as_str())
        .any(|s| {
            ["usage limit", "usagelimit", "credit", "daily limit"]
                .iter()
                .any(|word| s.to_lowercase().contains(word))
        })
}

fn structured(line: &str) -> Option<Marker> {
    if !line.starts_with('{') {
        return None;
    }
    let v: Value = serde_json::from_str(line).ok()?;
    match v["type"].as_str()? {
        "error" => match v["error"]["status"].as_i64()? {
            429 => {
                let e = &v["error"];
                let text = format!("{} {}", e["type"], e["message"]);
                Some(if quota(&text) {
                    Marker::Quota
                } else {
                    Marker::RateLimit
                })
            }
            502..=504 => Some(Marker::ProviderUnavailable),
            _ => None,
        },
        "rate_limit_event" => match v["rate_limit_info"]["status"].as_str() {
            Some(s) if s.starts_with("allowed") => None,
            Some(_) => Some(Marker::Quota),
            None if event_words(&v) => Some(Marker::Quota),
            None => None,
        },
        _ => None,
    }
}

fn text_marker(line: &str, stderr: bool) -> Option<Marker> {
    let p = patterns();
    let shaped = p.error.is_match(line);
    let candidate = shaped
        || (stderr
            && p.words
                .iter()
                .any(|re| re.find(line.trim_start()).is_some_and(|m| m.start() == 0)));
    p.words.iter().enumerate().find_map(|(n, re)| {
        re.find(line)?;
        let accepted = if n == 1 {
            stderr || shaped || p.error_json.is_match(line)
        } else {
            candidate
        };
        accepted.then_some(ORDER[n])
    })
}

/// Select by class priority, then first line in stdout-before-stderr order.
pub fn scan(i: &Input<'_>) -> Option<Hit> {
    let p = patterns();
    let mut best: Option<Hit> = None;
    for (stream, stderr) in [(i.stdout, false), (i.stderr, true)] {
        let normalized = p
            .ansi
            .replace_all(stream, "")
            .replace("\r\n", "\n")
            .replace('\r', "\n");
        for line in normalized.lines() {
            let event = line.starts_with('{')
                && serde_json::from_str::<Value>(line)
                    .ok()
                    .is_some_and(|v| v.is_object() && v["type"] == "rate_limit_event");
            let lower = line.to_lowercase();
            let agy =
                lower.contains("agy_error") && lower.contains("429") && lower.contains("resets in");
            let mut candidates = Vec::new();
            if !stderr || event {
                candidates.extend(structured(line));
            }
            if agy && !event {
                candidates.push(Marker::Quota);
            }
            if i.exit != 0 && !event {
                candidates.extend(text_marker(line, stderr));
            }
            for marker in candidates {
                let rank = |m| ORDER.iter().position(|x| *x == m).unwrap();
                if best
                    .as_ref()
                    .is_some_and(|b| rank(b.marker) <= rank(marker))
                {
                    continue;
                }
                let reset = reset::capture(i.headers, line, i.response_date, i.ts);
                let line: String = line.chars().take(300).collect();
                best = Some(Hit {
                    marker,
                    line,
                    reset,
                });
            }
        }
    }
    best
}
