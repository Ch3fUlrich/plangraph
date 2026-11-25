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

/// One row per writer round of a native run record: a round starts with a writer turn
/// (its `writing` stage, or its turn when no stage was booked) and holds the checks,
/// review and push that follow until the next turn. The key carries the start of the
/// turn, so a retry of the run rewrites the same rows.
pub fn rows(record: &Value) -> Vec<RoundRow> {
    let Some(run) = record["run_id"].as_str() else {
        return Vec::new();
    };
    let Some(stage_rows) = record["stage_timings"].as_array() else {
        return Vec::new();
    };
    let mut stages: Vec<(i64, i64, Cause)> = stage_rows
        .iter()
        .filter_map(|s| {
            let (from, to) = (s["started_at_ms"].as_i64()?, s["ended_at_ms"].as_i64()?);
            Some((from, to.max(from), stage_cause(s["stage"].as_str()?)))
        })
        .collect();
    if stages.is_empty() {
        return Vec::new();
    }
    let mut starts: Vec<i64> = stage_rows
        .iter()
        .filter(|s| s["stage"] == "writing")
        .filter_map(|s| s["started_at_ms"].as_i64())
        .collect();
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
    let turns: Vec<(i64, i64)> = record["writer_turns"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|t| Some((parse_ts(&t["started"])?, parse_ts(&t["ended"])?)))
        .collect();
    if starts.is_empty() {
        starts = turns.iter().map(|t| t.0).collect();
    }
    for (from, to) in turns {
        for (a, b) in free_parts(from, to.max(from), &owned.clone()) {
            booked.push((a, b, Cause::NativeTurn));
            owned.push((a, b));
            owned.sort_unstable();
        }
    }
    let first = booked.iter().map(|b| b.0).min().unwrap_or(0);
    starts.sort_unstable();
    starts.dedup();
    if starts.is_empty() {
        starts.push(first);
    }
    let last_round = starts.len() - 1;
    (0..starts.len())
        .filter_map(|i| {
            let from = if i == 0 {
                first.min(starts[0])
            } else {
                starts[i]
            };
            let until = starts.get(i + 1).copied().unwrap_or(i64::MAX);
            let mine: Vec<&(i64, i64, Cause)> = booked
                .iter()
                .filter(|b| b.0 >= from && b.0 < until)
                .collect();
            let begin = mine.iter().map(|b| b.0).min()?;
            let end = mine.iter().map(|b| b.1).max()?;
            let mut causes: HashMap<Cause, i64> = HashMap::new();
            for (a, b, cause) in mine {
                *causes.entry(*cause).or_insert(0) += b - a;
            }
            let covered: i64 = causes.values().sum();
            if end - begin > covered {
                *causes.entry(Cause::Unknown).or_insert(0) += end - begin - covered;
            }
            Some(RoundRow {
                round_key: format!("{run}:native:{}", starts[i]),
                index: i + 1,
                started_ms: begin,
                ended_ms: end,
                causes,
                outcome: (i == last_round)
                    .then(|| record["result"].as_str().map(str::to_owned))
                    .flatten(),
            })
        })
        .collect()
}
