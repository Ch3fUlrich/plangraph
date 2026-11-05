//! Pure row-derived health. Provider reset values and overlays belong to RL10b.
use crate::{hosts::Role, outcome::Class};

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
    /// Any kind marks an observation, not an attempt outcome.
    pub kind: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Closed,
    Open,
    HalfOpen,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UntilSrc {
    Row,
    Default,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Health {
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
    pub fn available(&self, now_s: i64) -> bool {
        self.state == State::Closed || self.until_s.is_none_or(|until| now_s >= until)
    }

    fn advance(&mut self, ts: i64) {
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
    let mut sorted = rows;
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
        if !row.outcome.is_neutral() && row.outcome != Class::Ok {
            if let Some(last) = out.last_mut() {
                if last.leg_key == row.leg_key
                    && last.outcome == row.outcome
                    && start(&row) <= last.ts
                {
                    let begin = start(last).min(start(&row));
                    last.ts = last.ts.max(row.ts);
                    last.duration_s = Some(last.ts.saturating_sub(begin));
                    continue;
                }
            }
        }
        out.push(row);
    }
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
/// Duplicate attempts use the last input row, as in the append-only evidence file.
pub fn fold(leg_key: &str, route_key: &str, role: Role, rows: &[Row], now_s: i64) -> Health {
    let mut attempts: Vec<Row> = Vec::new();
    for row in rows.iter().filter(|r| r.kind.is_none()) {
        if let Some(old) = attempts
            .iter_mut()
            .find(|r| r.run_id == row.run_id && r.attempt == row.attempt)
        {
            *old = row.clone();
        } else {
            attempts.push(row.clone());
        }
    }
    let rows = incidents(
        attempts
            .into_iter()
            .filter(|r| {
                r.leg_key == leg_key
                    && r.route_key == route_key
                    && (r.role == role || r.outcome != Class::Ok)
            })
            .collect(),
    );
    let mut health = Health::default();
    let mut success = None;
    for row in rows {
        health.advance(row.ts);
        let class = row.outcome;
        if class == Class::Ok {
            health = Health::default();
            success = Some(row.ts);
            continue;
        }
        if class.is_neutral() {
            continue;
        }
        health.non_verdict_count = 0;
        health.strikes = health.strikes.saturating_add(1);
        let probe = health.state == State::HalfOpen;
        let recent_success = success.is_some_and(|ts| row.ts.saturating_sub(ts) < 86400);
        if class.carries_reset()
            || ((probe || health.strikes >= trip(class)) && !(probe && recent_success))
        {
            health.open(class, row.ts);
        }
    }
    health.advance(now_s);
    health
}
