//! Pure chronological health replay, with optional observation-anchored reset state.
pub use crate::health_family::{family_floor, order_legs, LegInfo};
use crate::{hosts::Role, outcome::Class};

#[cfg(test)]
mod nonverdict_tests;
#[cfg(test)]
mod tests;

#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub leg_key: String,
    pub route_key: String,
    pub ts: i64,
    pub role: Role,
    pub outcome: Class,
    pub run_id: String,
    pub attempt: u32,
    pub duration_s: Option<i64>,
    pub reset_at: Option<i64>,
    pub reset_assumed: bool,
    pub reset_clamped: bool,
    /// Only canary and omniroute_* kinds mark non-outcome observations.
    pub kind: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Closed,
    Degraded,
    Open,
    HalfOpen,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UntilSrc {
    Row,
    Default,
    OmniRoute,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Health {
    pub review_unfit: bool,
    pub state: State,
    pub until_s: Option<i64>,
    pub until_src: Option<UntilSrc>,
    pub reason: Option<Class>,
    pub strikes: u32,
    pub heuristic: bool,
    pub reset_clamped: bool,
    pub reset_assumed: bool,
    pub n: u32,
    pub non_verdict_count: u32,
}
impl Default for Health {
    fn default() -> Self {
        Self {
            review_unfit: false,
            state: State::Closed,
            until_s: None,
            until_src: None,
            reason: None,
            strikes: 0,
            heuristic: false,
            reset_clamped: false,
            reset_assumed: false,
            n: 0,
            non_verdict_count: 0,
        }
    }
}
impl Health {
    pub(crate) fn clear_on_ok(&mut self, role: Role) {
        let review_unfit = role != Role::Review && self.review_unfit;
        *self = Self::default();
        self.review_unfit = review_unfit;
    }

    pub fn available(&self, now_s: i64) -> bool {
        matches!(self.state, State::Closed | State::Degraded)
            || self.until_s.is_none_or(|until| now_s >= until)
    }

    pub(crate) fn advance(&mut self, ts: i64) {
        if self.state == State::Open && self.until_s.is_some_and(|until| ts >= until) {
            self.state = State::HalfOpen;
            self.non_verdict_count = 0;
        }
    }

    fn open(&mut self, class: Class, ts: i64) {
        self.n = self.n.saturating_add(1);
        self.state = State::Open;
        self.reason = Some(class);
        self.heuristic = !class.carries_reset();
        self.reset_assumed = class == Class::Quota;
        self.reset_clamped = false;
        self.until_src = Some(UntilSrc::Default);
        self.until_s = Some(if class == Class::Quota {
            next_utc_midnight(ts)
        } else {
            ts.saturating_add(backoff_s(self.n))
        });
        self.non_verdict_count = 0;
    }
}

pub fn backoff_s(n: u32) -> i64 {
    300_i64
        .saturating_mul(1_i64 << n.saturating_sub(1).min(5))
        .min(7200)
}
pub fn next_utc_midnight(ts: i64) -> i64 {
    ts.div_euclid(86400).saturating_add(1).saturating_mul(86400)
}
pub fn reprobe_gap_s() -> i64 {
    60
}

/// All counted incidents have weight one; EMPTY and CRASH share trip two.
fn trip(class: Class) -> u32 {
    match class {
        Class::Empty | Class::Crash | Class::KilledExternal => 2,
        Class::NoProgress | Class::NoVerdict => 3,
        _ => 1,
    }
}

fn start(row: &Row) -> i64 {
    row.ts.saturating_sub(row.duration_s.unwrap_or(0).max(0))
}

/// Coalesce interval chains per leg and class before chronological replay.
fn incidents(rows: Vec<Row>) -> Vec<Row> {
    let (mut sorted, observations): (Vec<_>, Vec<_>) =
        rows.into_iter().partition(|r| r.kind.is_none());
    sorted.sort_by(|a, b| {
        (
            &a.leg_key,
            a.outcome.as_str(),
            start(a),
            a.ts,
            &a.run_id,
            a.attempt,
        )
            .cmp(&(
                &b.leg_key,
                b.outcome.as_str(),
                start(b),
                b.ts,
                &b.run_id,
                b.attempt,
            ))
    });
    let mut out: Vec<Row> = Vec::new();
    for row in sorted {
        if row.kind.is_none() && !row.outcome.is_neutral() && row.outcome != Class::Ok {
            if let Some(last) = out.last_mut() {
                if last.kind.is_none()
                    && last.leg_key == row.leg_key
                    && last.outcome == row.outcome
                    && start(&row) <= last.ts
                {
                    let begin = start(last).min(start(&row));
                    if row.reset_at.is_some()
                        && (row.ts, &row.run_id, row.attempt, row.reset_at)
                            >= (last.ts, &last.run_id, last.attempt, last.reset_at)
                    {
                        last.reset_at = row.reset_at;
                    }
                    last.ts = last.ts.max(row.ts);
                    last.duration_s = Some(last.ts.saturating_sub(begin));
                    continue;
                }
            }
        }
        out.push(row);
    }
    out.extend(observations);
    out.sort_by(|a, b| {
        (
            a.ts,
            a.outcome == Class::Ok,
            a.outcome.as_str(),
            &a.run_id,
            a.attempt,
        )
            .cmp(&(
                b.ts,
                b.outcome == Class::Ok,
                b.outcome.as_str(),
                &b.run_id,
                b.attempt,
            ))
    });
    out
}

/// Fold one leg and route. Times are integral Unix seconds supplied by the caller.
/// Routes are fanned out by the caller; `fold` sees one route.
/// Only the requested leg, route and role contribute, for successes and failures alike.
/// Duplicate attempts keep the greatest (timestamp, class, duration) tuple;
/// Observations and future rows are excluded before attempt dedupe.
pub fn fold(leg_key: &str, route_key: &str, role: Role, rows: &[Row], now_s: i64) -> Health {
    replay_inner(leg_key, route_key, role, rows, now_s, None, false)
}

pub(crate) fn replay(
    leg_key: &str,
    route_key: &str,
    role: Role,
    rows: &[Row],
    now_s: i64,
    provider: Option<&crate::health_until::ProviderState>,
) -> Health {
    replay_inner(leg_key, route_key, role, rows, now_s, provider, true)
}

#[allow(clippy::too_many_arguments)]
fn replay_inner(
    leg_key: &str,
    route_key: &str,
    role: Role,
    rows: &[Row],
    now_s: i64,
    provider: Option<&crate::health_until::ProviderState>,
    response_resets: bool,
) -> Health {
    use crate::health_until::ReplayReset;
    let mut attempts: Vec<Row> = Vec::new();
    let mut neutral_rows = Vec::new();
    for row in rows.iter().filter(|r| {
        r.leg_key == leg_key && r.route_key == route_key && r.role == role && r.ts <= now_s
    }) {
        if row
            .kind
            .as_deref()
            .is_some_and(|kind| kind == "canary" || kind.starts_with("omniroute_"))
        {
            continue;
        }
        if row.kind.is_some() {
            neutral_rows.push(row.clone());
            continue;
        }
        if let Some(old) = attempts
            .iter_mut()
            .find(|r| r.run_id == row.run_id && r.attempt == row.attempt)
        {
            if (row.ts, row.outcome.as_str(), row.duration_s, row.reset_at)
                > (old.ts, old.outcome.as_str(), old.duration_s, old.reset_at)
            {
                *old = row.clone();
            }
        } else {
            attempts.push(row.clone());
        }
    }
    let evidence = attempts.clone();
    attempts.extend(neutral_rows);
    let rows = incidents(attempts);
    let mut health = Health::default();
    let mut resets = ReplayReset::new(provider.filter(|p| p.observed_s <= now_s));
    let mut success = None;
    let mut killed = Vec::new();
    for row in rows {
        resets.before(&mut health, row.ts);
        health.advance(row.ts);
        let class = row.outcome;
        if class == Class::Ok && row.kind.is_none() {
            resets.success();
            health.clear_on_ok(role);
            success = Some(row.ts);
            killed.clear();
            continue;
        }
        if class.is_neutral() || row.kind.is_some() {
            if health.state == State::HalfOpen {
                health.non_verdict_count += 1;
                if health.non_verdict_count == 3 {
                    health.open(class, row.ts);
                    health.heuristic = false;
                } else {
                    health.until_s = Some(row.ts.saturating_add(reprobe_gap_s()));
                    health.until_src = Some(UntilSrc::Default);
                }
            }
            continue;
        }
        if class == Class::ToolCallOnly && role == Role::Review {
            health.review_unfit = true;
        }
        health.non_verdict_count = 0;
        health.strikes = health.strikes.saturating_add(1);
        let reached = if class == Class::KilledExternal {
            killed.retain(|ts| row.ts.saturating_sub(*ts) <= 3600);
            killed.push(row.ts);
            killed.len() >= trip(class) as usize
        } else {
            health.strikes >= trip(class)
        };
        let probe = health.state == State::HalfOpen;
        let recent_success = success.is_some_and(|ts| row.ts.saturating_sub(ts) < 86400);
        // A recent success suppresses non-reset probe failures for 24 hours.
        // Further incidents while OPEN intentionally escalate n and restart the deadline.
        if class.carries_reset() || ((probe || reached) && !(probe && recent_success)) {
            health.open(class, row.ts);
            let reset = evidence
                .iter()
                .filter(|r| {
                    r.outcome == class
                        && r.reset_at.is_some()
                        && r.ts >= start(&row)
                        && r.ts <= row.ts
                        && start(r) <= row.ts
                })
                .max_by_key(|r| (r.ts, &r.run_id, r.attempt, r.reset_at));
            if response_resets {
                resets.open(&mut health, &row, reset, probe);
            }
        }
    }
    resets.finish(&mut health, now_s);
    health.advance(now_s);
    health
}
