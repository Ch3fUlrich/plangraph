//! ChatGPT pool use per window: measure it, flag free capacity that ready work could
//! take, and say which runs to start. Pure check over one injected sample; the caller
//! (flow watchdog, `pool-use-check`) owns delivery and the dispatcher hook.
use serde::Deserialize;
use std::{collections::BTreeMap, fs, io::Write, path::Path};

/// A pool counts as under-used below this share of the pace that spends the window by its reset.
pub const PACE_SHARE: f64 = 0.7;
/// Telemetry rows are at most one per this many seconds, however often the check runs.
pub const ROW_SPACING_S: i64 = 600;

/// One window of one pool, as the collector read it. `live` and `waiting` count runs
/// and ready work per leg; `admission_slots` is what host admission still allows.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pool {
    pub pool: String,
    pub window: String,
    pub now: i64,
    pub window_start: i64,
    pub reset_at: i64,
    pub used_pct: f64,
    #[serde(default)]
    pub live: BTreeMap<String, u64>,
    #[serde(default)]
    pub waiting: BTreeMap<String, u64>,
    #[serde(default)]
    pub admission_slots: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Report {
    pub pool: String,
    pub window: String,
    pub now: i64,
    pub reset_at: i64,
    pub used_pct: f64,
    pub pace_pct: f64,
    /// The sample's window already ended: a stale read never raises an alert.
    pub stale: bool,
    pub underused: bool,
    pub live: BTreeMap<String, u64>,
    pub waiting: BTreeMap<String, u64>,
    pub waiting_total: u64,
    pub startable: u64,
    /// Host admission the sample offered; shared by every window of one tick.
    pub admission_slots: u64,
    /// `(leg, runs)` to start now, at most `startable` runs in total.
    pub starts: Vec<(String, u64)>,
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_.:/".contains(c))
}

impl Pool {
    /// Bad samples are errors, never fabricated free capacity.
    pub fn report(&self) -> Result<Report, &'static str> {
        let legs_ok = self
            .live
            .keys()
            .chain(self.waiting.keys())
            .all(|k| identifier(k));
        if !identifier(&self.pool)
            || !identifier(&self.window)
            || !legs_ok
            || !self.used_pct.is_finite()
            || !(0.0..=100.0).contains(&self.used_pct)
            || self.reset_at <= self.window_start
            || self.now < self.window_start
        {
            return Err("invalid pool use sample");
        }
        let stale = self.now >= self.reset_at;
        let span = (self.reset_at - self.window_start) as f64;
        let pace_pct = (((self.now - self.window_start) as f64 / span) * 100.0).clamp(0.0, 100.0);
        let waiting_total: u64 = self.waiting.values().sum();
        let underused = !stale && self.used_pct < PACE_SHARE * pace_pct;
        let startable = if underused {
            waiting_total.min(self.admission_slots)
        } else {
            0
        };
        let starts = round_robin(&self.waiting, startable);
        Ok(Report {
            pool: self.pool.clone(),
            window: self.window.clone(),
            now: self.now,
            reset_at: self.reset_at,
            used_pct: self.used_pct,
            pace_pct,
            stale,
            underused,
            live: self.live.clone(),
            waiting: self.waiting.clone(),
            waiting_total,
            startable,
            admission_slots: self.admission_slots,
            starts,
        })
    }
}

/// Round robin over the legs by name keeps the split deterministic and fair.
fn round_robin(waiting: &BTreeMap<String, u64>, mut budget: u64) -> Vec<(String, u64)> {
    let mut left = waiting.clone();
    let mut taken: BTreeMap<String, u64> = BTreeMap::new();
    while budget > 0 && left.values().any(|n| *n > 0) {
        for (leg, n) in left.iter_mut().filter(|(_, n)| **n > 0) {
            if budget == 0 {
                break;
            }
            *n -= 1;
            budget -= 1;
            *taken.entry(leg.clone()).or_default() += 1;
        }
    }
    taken.into_iter().collect()
}

fn per_leg(map: &BTreeMap<String, u64>) -> String {
    if map.is_empty() {
        return "-".into();
    }
    map.iter()
        .map(|(leg, n)| format!("{leg}={n}"))
        .collect::<Vec<_>>()
        .join(",")
}

impl Report {
    /// Digest line: pool used %, runs live per leg, work waiting per leg.
    pub fn digest_line(&self) -> String {
        format!(
            "POOL {} window={} used={:.0}% pace={:.0}%{} live[{}] waiting[{}]",
            self.pool,
            self.window,
            self.used_pct,
            self.pace_pct,
            if self.stale { " stale" } else { "" },
            per_leg(&self.live),
            per_leg(&self.waiting)
        )
    }

    /// One alert while free capacity and ready work coexist.
    pub fn alert(&self) -> Option<String> {
        (self.underused && self.waiting_total > 0).then(|| {
            format!(
                "POOL-UNDERUSE {} window={} used={:.0}% pace={:.0}% waiting={} startable={}",
                self.pool,
                self.window,
                self.used_pct,
                self.pace_pct,
                self.waiting_total,
                self.startable
            )
        })
    }

    /// The identity under which the alert stays standing until the pool recovers.
    pub fn alert_key(&self) -> String {
        format!("pool-underuse {} {}", self.pool, self.window)
    }

    pub fn telemetry_row(&self) -> serde_json::Value {
        serde_json::json!({"ts":self.now,"pool":self.pool,"window":self.window,
            "reset_at":self.reset_at,"used_pct":self.used_pct,"pace_pct":self.pace_pct,
            "underused":self.underused,"waiting":self.waiting_total,"startable":self.startable,
            "live":self.live.values().sum::<u64>()})
    }
}

/// The dispatcher's input for one tick: the runs to start now over every under-used
/// window, or `None` when there are none. The windows of a pool see the same ready work
/// and draw on the same host admission, so work is counted once (the largest per-leg
/// count) and the tick's total never exceeds the smallest admission any sample offered.
pub fn fill_event(reports: &[Report]) -> Option<serde_json::Value> {
    let live: Vec<&Report> = reports.iter().filter(|r| r.underused).collect();
    let budget = live.iter().map(|r| r.admission_slots).min()?;
    let mut waiting: BTreeMap<String, u64> = BTreeMap::new();
    for report in &live {
        for (leg, n) in &report.waiting {
            let seen = waiting.entry(leg.clone()).or_default();
            *seen = (*seen).max(*n);
        }
    }
    let starts = round_robin(&waiting, budget);
    (!starts.is_empty()).then(|| {
        serde_json::json!({"kind":"pool-fill",
            "windows":live.iter().map(|r| format!("{} {}", r.pool, r.window)).collect::<Vec<_>>(),
            "starts":starts.iter().map(|(leg,n)| serde_json::json!({"leg":leg,"runs":n}))
                .collect::<Vec<_>>()})
    })
}

/// Append the report as one JSON line unless the file's newest row for the same
/// pool and window is younger than [`ROW_SPACING_S`]. Returns whether a row was written.
pub fn record(path: &Path, report: &Report) -> std::io::Result<bool> {
    let newest = fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .rev()
        .filter_map(|l| serde_json::from_str::<serde_json::Value>(l).ok())
        .find(|r| r["pool"] == report.pool.as_str() && r["window"] == report.window.as_str())
        .and_then(|r| r["ts"].as_i64());
    if newest.is_some_and(|ts| report.now >= ts && report.now - ts < ROW_SPACING_S) {
        return Ok(false);
    }
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(file, "{}", report.telemetry_row())?;
    Ok(true)
}
