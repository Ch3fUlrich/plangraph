//! The parallel-trains experiment (D-6774): over `days`, batches and items merged per hour, full test runs
//! per landed batch and red trains per day, against the same measures over the `days` before. The
//! experiment is kept when batches per hour rise. Read from the stage ledger, no other source.
use super::timeline::Stage;
use std::collections::BTreeSet;

const DAY: i64 = 86_400;

#[derive(Debug, PartialEq)]
pub struct Numbers {
    pub landed: usize,
    pub items: u32,
    /// Landed batches whose `merge` row carries no item count; their items are not in `items`.
    pub items_unknown: usize,
    pub batches_per_hour: f64,
    pub items_per_hour: f64,
    /// Full CI runs (distinct heads with CI rows) of the landed batches, per landed batch.
    pub test_runs_per_batch: f64,
    pub red_trains: usize,
    pub red_per_day: f64,
}

#[derive(Debug, PartialEq)]
pub struct Comparison {
    pub days: i64,
    pub recent: Numbers,
    pub previous: Numbers,
}

impl Comparison {
    /// Keep the experiment when batches per hour rise against the window before.
    pub fn keep(&self) -> bool {
        self.recent.batches_per_hour > self.previous.batches_per_hour
    }
}

fn red(stage: &Stage) -> bool {
    stage.stage.starts_with("ci:") && matches!(stage.result.as_str(), "failure" | "timed_out")
}

/// The numbers for merges and red runs that ended in `[from, to)`.
pub fn measure(stages: &[Stage], from: i64, to: i64) -> Numbers {
    let inside = |s: &&Stage| (from..to).contains(&s.end);
    let landed: Vec<&Stage> = stages
        .iter()
        .filter(|s| s.stage == "merge" && s.result == "success")
        .filter(inside)
        .collect();
    let subjects: BTreeSet<&str> = landed.iter().map(|s| s.subject.as_str()).collect();
    let runs: BTreeSet<(&str, &str)> = stages
        .iter()
        .filter(|s| s.stage.starts_with("ci:") && subjects.contains(s.subject.as_str()))
        .map(|s| (s.subject.as_str(), s.head.as_str()))
        .collect();
    let red_heads: BTreeSet<(&str, &str)> = stages
        .iter()
        .filter(|s| red(s))
        .filter(inside)
        .map(|s| (s.subject.as_str(), s.head.as_str()))
        .collect();
    let hours = (to - from) as f64 / 3600.0;
    let days = (to - from) as f64 / DAY as f64;
    let items: u32 = landed.iter().filter_map(|s| s.items).sum();
    Numbers {
        landed: landed.len(),
        items,
        items_unknown: landed.iter().filter(|s| s.items.is_none()).count(),
        batches_per_hour: landed.len() as f64 / hours,
        items_per_hour: f64::from(items) / hours,
        test_runs_per_batch: if subjects.is_empty() {
            0.0
        } else {
            runs.len() as f64 / subjects.len() as f64
        },
        red_trains: red_heads.len(),
        red_per_day: red_heads.len() as f64 / days,
    }
}

/// The last `days` days up to `now` against the `days` before them.
pub fn compare(stages: &[Stage], now: i64, days: i64) -> Comparison {
    let span = days.max(1) * DAY;
    Comparison {
        days: days.max(1),
        recent: measure(stages, now - span, now),
        previous: measure(stages, now - 2 * span, now - span),
    }
}

pub fn render(c: &Comparison) -> Vec<String> {
    let line = |label: &str, n: &Numbers| {
        format!(
            "{label} ({}d): {} batches, {:.3}/h; {} items, {:.3}/h ({} batches without item count); {:.2} test runs per landed batch; {} red trains, {:.2}/day",
            c.days,
            n.landed,
            n.batches_per_hour,
            n.items,
            n.items_per_hour,
            n.items_unknown,
            n.test_runs_per_batch,
            n.red_trains,
            n.red_per_day,
        )
    };
    vec![
        line("recent", &c.recent),
        line("previous", &c.previous),
        format!(
            "batches per hour {}: {}",
            if c.keep() { "rose" } else { "did not rise" },
            if c.keep() { "keep" } else { "drop" }
        ),
    ]
}
