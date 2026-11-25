//! Round row of a native Codex writer run, from the intervals the supervisor already books.
//!
//! The run record holds `stage_timings` (writing, fast_checks, review, final_gates, push)
//! and `writer_turns`. The `writing` stage and a writer turn are the same enclosing
//! interval: its tools and waits are not split. Every instant is booked once: a stage
//! owns its interval, a turn owns what no stage covers, and uncovered time is unknown.

use super::round_time::{Cause, RoundRow};
use serde_json::Value;
use std::collections::HashMap;

fn stage_cause(stage: &str) -> Cause {
    match stage {
        "writing" => Cause::NativeTurn,
        "fast_checks" | "final_gates" => Cause::Gate,
        "review" => Cause::Review,
        "push" => Cause::Push,
        _ => Cause::Unknown,
    }
}

fn parse_ts(value: &Value) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(value.as_str()?)
        .ok()
        .map(|t| t.timestamp_millis())
}

/// Parts of `[start, end)` that the sorted, disjoint `taken` intervals leave free.
fn free_parts(start: i64, end: i64, taken: &[(i64, i64)]) -> Vec<(i64, i64)> {
    let mut parts = Vec::new();
    let mut cursor = start;
    for &(from, to) in taken {
        if to <= cursor || from >= end {
            continue;
        }
        if from > cursor {
            parts.push((cursor, from.min(end)));
        }
        cursor = cursor.max(to);
    }
    if cursor < end {
        parts.push((cursor, end));
    }
    parts
}

/// The one row of a native run record, or none when it carries no stage interval.
pub fn row(record: &Value) -> Option<RoundRow> {
    let run = record["run_id"].as_str()?;
    let mut stages: Vec<(i64, i64, Cause)> = record["stage_timings"]
        .as_array()?
        .iter()
        .filter_map(|s| {
            let (from, to) = (s["started_at_ms"].as_i64()?, s["ended_at_ms"].as_i64()?);
            Some((from, to.max(from), stage_cause(s["stage"].as_str()?)))
        })
        .collect();
    if stages.is_empty() {
        return None;
    }
    stages.sort_by_key(|s| (s.0, s.1));
    // A later stage never re-books time an earlier one owns.
    let mut owned: Vec<(i64, i64)> = Vec::new();
    let mut booked: Vec<(i64, i64, Cause)> = Vec::new();
    for (from, to, cause) in stages {
        for (a, b) in free_parts(from, to, &owned) {
            booked.push((a, b, cause));
            owned.push((a, b));
            owned.sort_unstable();
        }
    }
    for turn in record["writer_turns"].as_array().into_iter().flatten() {
        let (Some(from), Some(to)) = (parse_ts(&turn["started"]), parse_ts(&turn["ended"])) else {
            continue;
        };
        for (a, b) in free_parts(from, to.max(from), &owned.clone()) {
            booked.push((a, b, Cause::NativeTurn));
            owned.push((a, b));
            owned.sort_unstable();
        }
    }
    let first = booked.iter().map(|b| b.0).min()?;
    let last = booked.iter().map(|b| b.1).max()?;
    let mut causes: HashMap<Cause, i64> = HashMap::new();
    for (a, b, cause) in &booked {
        *causes.entry(*cause).or_insert(0) += b - a;
    }
    let covered: i64 = causes.values().sum();
    if last - first > covered {
        *causes.entry(Cause::Unknown).or_insert(0) += last - first - covered;
    }
    Some(RoundRow {
        round_key: format!("{run}:native"),
        index: 1,
        started_ms: first,
        ended_ms: last,
        causes,
        outcome: record["result"].as_str().map(str::to_owned),
    })
}
