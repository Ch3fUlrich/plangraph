//! RL18a: OmniRoute provider state, read live and never stored.
//!
//! One GET answers `{"circuitBreakers":[{name,state,retryAfterMs,changedAt?}],
//! "windows":[{models:[..],rateLimitedUntil,changedAt?}]}`. A window lists every
//! model that shares it, so one shared pool opens each member while two separate
//! windows stay separate. Values are used as read and never clamped; the
//! conversion is `reset::capture`. Unreachable, refused or stale reads yield `None`.

use pg_worker_core::{health_until::ProviderState, reset};
use serde_json::Value;
use std::collections::BTreeMap;

pub(crate) const TTL_S: i64 = 60;
const STALE_S: i64 = 2 * TTL_S;
const NO_TIME_S: i64 = 86_400;
/// Re-reads of one relative `retryAfterMs` differ by rounding only.
const SAME_UNTIL_S: i64 = 2;

pub(crate) struct Fetched {
    pub body: String,
    pub date: Option<String>,
}

pub(crate) trait Fetch {
    fn get(&self) -> Result<Fetched, ()>;
}

pub(crate) struct Http {
    url: String,
    token: Option<String>,
    agent: ureq::Agent,
}

impl Http {
    pub(crate) fn new(base: &str, token: Option<String>) -> Self {
        let agent = ureq::Agent::config_builder()
            .proxy(None)
            .max_redirects(0)
            .timeout_global(Some(std::time::Duration::from_secs(5)))
            .build()
            .into();
        let url = format!("{}/api/monitoring/health", base.trim_end_matches('/'));
        Self { url, token, agent }
    }
}

impl Fetch for Http {
    fn get(&self) -> Result<Fetched, ()> {
        let mut request = self.agent.get(&self.url);
        if let Some(token) = &self.token {
            request = request.header("Authorization", format!("Bearer {}", token.trim()));
        }
        let mut response = request.call().map_err(|_| ())?;
        let date = response
            .headers()
            .get("date")
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned);
        let body = response
            .body_mut()
            .with_config()
            .limit(1 << 20)
            .read_to_string()
            .map_err(|_| ())?;
        Ok(Fetched { body, date })
    }
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Kind {
    Breaker,
    Window,
}

pub(crate) struct Reader<F> {
    fetch: F,
    tried_at: Option<i64>,
    good_at: Option<i64>,
    states: BTreeMap<String, ProviderState>,
    /// `observed_s` stays at the first read that saw this value.
    seen: BTreeMap<(String, Kind), ProviderState>,
}

impl<F: Fetch> Reader<F> {
    pub(crate) fn new(fetch: F) -> Self {
        Self {
            fetch,
            tried_at: None,
            good_at: None,
            states: BTreeMap::new(),
            seen: BTreeMap::new(),
        }
    }

    /// Per-leg-key states as of `now`, re-read at most every `TTL_S`.
    pub(crate) fn read(&mut self, now: i64) -> Option<&BTreeMap<String, ProviderState>> {
        if self.tried_at.is_none_or(|t| now - t >= TTL_S || now < t) {
            self.tried_at = Some(now);
            if let Some(fetched) = self.fetch.get().ok().and_then(|f| parse(&f, now)) {
                self.apply(fetched, now);
            }
        }
        self.good_at
            .is_some_and(|t| (0..STALE_S).contains(&(now - t)))
            .then_some(&self.states)
    }

    fn apply(&mut self, entries: Vec<Entry>, now: i64) {
        let mut seen = BTreeMap::new();
        let mut states: BTreeMap<String, ProviderState> = BTreeMap::new();
        for (leg, kind, until, changed) in entries {
            let key = (leg.clone(), kind);
            let same = |s: &ProviderState| match (s.until, until) {
                (Some(a), Some(b)) => (a - b).abs() <= SAME_UNTIL_S,
                (a, b) => a == b,
            };
            let state = match (changed, self.seen.remove(&key)) {
                (Some(at), _) => ProviderState {
                    until,
                    observed_s: at.min(now),
                },
                (None, Some(old)) if same(&old) => old,
                (None, _) => ProviderState {
                    until,
                    observed_s: now,
                },
            };
            let deadline = |s: &ProviderState| s.until.unwrap_or(s.observed_s + NO_TIME_S);
            if states
                .get(&leg)
                .is_none_or(|best| deadline(&state) > deadline(best))
            {
                states.insert(leg, state.clone());
            }
            seen.insert(key, state);
        }
        (self.seen, self.states, self.good_at) = (seen, states, Some(now));
    }
}

/// (leg key, source, `until`, OmniRoute's change time)
type Entry = (String, Kind, Option<i64>, Option<i64>);

fn parse(fetched: &Fetched, now: i64) -> Option<Vec<Entry>> {
    let body: Value = serde_json::from_str(&fetched.body).ok()?;
    let date = fetched.date.as_deref().and_then(reset::http_date);
    let (breakers, windows) = (body.get("circuitBreakers"), body.get("windows"));
    if breakers.is_none() && windows.is_none() {
        return None;
    }
    let mut out = Vec::new();
    for entry in breakers.and_then(Value::as_array).into_iter().flatten() {
        if entry["state"] != "OPEN" {
            continue;
        }
        let Some(name) = entry["name"].as_str() else {
            continue;
        };
        let text = entry["retryAfterMs"]
            .as_f64()
            .map(|ms| format!("{{\"retryAfterMs\": {ms}}}"))
            .unwrap_or_default();
        let until = reset::capture(&[], &text, date, now as f64).map(|r| r.reset_at.round() as i64);
        out.push((name.to_owned(), Kind::Breaker, until, changed(entry, now)));
    }
    for entry in windows.and_then(Value::as_array).into_iter().flatten() {
        let given = match &entry["rateLimitedUntil"] {
            Value::String(s) if !s.trim().is_empty() => s.trim().to_owned(),
            Value::Number(n) => {
                let n = n.as_f64()?;
                (if n >= 1e12 { n / 1000.0 } else { n }).to_string()
            }
            _ => continue,
        };
        let header = [("x-ratelimit-reset", given.as_str())];
        let until =
            reset::capture(&header, "", date, now as f64).map(|r| r.reset_at.round() as i64);
        for model in entry["models"].as_array().into_iter().flatten() {
            if let Some(model) = model.as_str() {
                out.push((model.to_owned(), Kind::Window, until, changed(entry, now)));
            }
        }
    }
    Some(out)
}

/// OmniRoute's own change time (epoch seconds, epoch ms or a date string).
fn changed(entry: &Value, now: i64) -> Option<i64> {
    let at = match &entry["changedAt"] {
        Value::Number(n) => n.as_f64().map(|n| if n >= 1e12 { n / 1000.0 } else { n }),
        Value::String(s) => reset::http_date(s),
        _ => None,
    }?;
    (at > 0.0).then(|| (at as i64).min(now))
}

#[cfg(test)]
#[path = "provider_state_tests.rs"]
mod tests;
