//! How many items one batch brief carries: computed from the writer model and the items'
//! expected size, never chosen by hand (operator 2026-11-20).
//!
//! The count is the smallest of three bounds. (a) The model cap: 10 for a strong writer
//! (catalog intelligence 50 or more) with a context window of 400k tokens or more, 5 for a
//! strong writer with a smaller window, 3 otherwise. (b) The fit: the items' expected diff
//! bytes, taken in order, stay under a planning target below the hard limit so the fix rounds
//! still fit; an item expected above half the limit goes alone. (c) The feedback of the last
//! batch: one item fewer after skipped items or a HIGH that survived two rounds, one more
//! after a batch accepted in round 1.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[cfg(test)]
#[path = "batch_size_tests.rs"]
mod tests;

/// Catalog intelligence of a strong writer (rule 3.41).
pub const STRONG_INTELLIGENCE: f64 = 50.0;
/// Context window, in tokens, from which a strong writer carries the largest batch.
pub const LARGE_WINDOW: u64 = 400_000;
/// Interim hard limit of a checkpoint diff; the ledger replaces it with a measured value.
pub const DEFAULT_LIMIT_BYTES: u64 = 100_000;
/// The planning target is this share of the limit, so the fix rounds still fit under it.
pub const TARGET_PERCENT: u64 = 60;
/// Expected diff bytes of an item with no estimate and no merged item of any kind to take a median of.
pub const UNKNOWN_ITEM_BYTES: u64 = 30_000;

#[derive(Debug, Deserialize)]
pub struct Writer {
    pub aa_intelligence: Option<f64>,
    pub context_window: Option<u64>,
}

#[derive(Debug, Deserialize)]
pub struct Item {
    pub key: String,
    pub kind: String,
    pub expected_bytes: Option<u64>,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    /// Items were skipped.
    Skipped,
    /// A HIGH finding survived two rounds.
    HighSurvived,
    /// The batch was accepted in round 1.
    AcceptedRound1,
    Neutral,
}

#[derive(Debug, Deserialize)]
pub struct Last {
    pub count: usize,
    pub outcome: Outcome,
}

#[derive(Debug, Deserialize)]
pub struct Input {
    pub writer: Writer,
    pub items: Vec<Item>,
    /// Diff bytes of merged items by kind: the fallback estimate is their median.
    #[serde(default)]
    pub merged_bytes: BTreeMap<String, Vec<u64>>,
    pub limit_bytes: Option<u64>,
    pub last: Option<Last>,
}

#[derive(Debug, PartialEq, Serialize)]
pub struct Plan {
    pub count: usize,
    pub keys: Vec<String>,
    /// Items expected above half the limit: each goes alone, in its own batch.
    pub alone: Vec<String>,
    pub reason: String,
}

/// Model cap (a): 10, 5 or 3 items.
pub fn model_cap(writer: &Writer) -> (usize, &'static str) {
    let strong = writer
        .aa_intelligence
        .is_some_and(|n| n.is_finite() && n >= STRONG_INTELLIGENCE);
    match (strong, writer.context_window.unwrap_or(0) >= LARGE_WINDOW) {
        (true, true) => (10, "strong writer, window of 400k or more"),
        (true, false) => (5, "strong writer, window below 400k"),
        _ => (3, "writer below the strong threshold"),
    }
}

fn median(values: &[u64]) -> Option<u64> {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    match sorted.len() {
        0 => None,
        n if n % 2 == 1 => Some(sorted[n / 2]),
        n => Some((sorted[n / 2 - 1] + sorted[n / 2]) / 2),
    }
}

/// Expected diff bytes: the item's estimate, else the median of merged items of its kind, else
/// the median of all merged items, else `UNKNOWN_ITEM_BYTES`.
pub fn expected_bytes(item: &Item, merged: &BTreeMap<String, Vec<u64>>) -> u64 {
    item.expected_bytes
        .or_else(|| merged.get(&item.kind).and_then(|v| median(v)))
        .or_else(|| median(&merged.values().flatten().copied().collect::<Vec<_>>()))
        .unwrap_or(UNKNOWN_ITEM_BYTES)
}

/// Feedback bound (c): the last batch's count moved by one, or no bound without a last batch.
pub fn feedback_bound(last: Option<&Last>) -> (usize, &'static str) {
    match last {
        None => (usize::MAX, "no earlier batch"),
        Some(Last { count, outcome }) => match outcome {
            Outcome::Skipped => (
                count.saturating_sub(1).max(1),
                "last batch had skipped items: one fewer",
            ),
            Outcome::HighSurvived => (
                count.saturating_sub(1).max(1),
                "last batch kept a HIGH over two rounds: one fewer",
            ),
            Outcome::AcceptedRound1 => (count + 1, "last batch accepted in round 1: one more"),
            Outcome::Neutral => (*count, "last batch neutral: same count"),
        },
    }
}

/// The batch to brief: how many of `input.items`, in the given order, and why.
pub fn plan(input: &Input) -> Plan {
    let limit = input.limit_bytes.unwrap_or(DEFAULT_LIMIT_BYTES);
    let target = limit * TARGET_PERCENT / 100;
    let (cap, cap_why) = model_cap(&input.writer);
    let (feedback, feedback_why) = feedback_bound(input.last.as_ref());
    let bound = cap.min(feedback);
    let (mut keys, mut alone, mut bytes) = (Vec::new(), Vec::new(), 0u64);
    let mut fit_stop = None;
    for (index, item) in input.items.iter().enumerate() {
        let expected = expected_bytes(item, &input.merged_bytes);
        if expected > limit / 2 {
            if index == 0 {
                // The first item is too big to share a batch: it is the batch.
                keys.push(item.key.clone());
                bytes = expected;
                break;
            }
            alone.push(item.key.clone());
            continue;
        }
        if keys.len() >= bound {
            break;
        }
        if bytes + expected > target {
            fit_stop = Some(item.key.clone());
            break;
        }
        bytes += expected;
        keys.push(item.key.clone());
    }
    let count = keys.len();
    let reason = format!(
        "{count} item(s): model cap {cap} ({cap_why}); feedback {} ({feedback_why}); fit {bytes} of {target} target bytes (limit {limit}){}{}",
        if feedback == usize::MAX { "none".to_string() } else { feedback.to_string() },
        fit_stop.map_or(String::new(), |k| format!(", {k} would pass the target")),
        if alone.is_empty() { String::new() } else { format!("; alone: {}", alone.join(" ")) },
    );
    Plan {
        count,
        keys,
        alone,
        reason,
    }
}

/// `plangraph batch-size INPUT.json`: print the plan as JSON.
pub fn run(args: &[String]) -> i32 {
    let [path] = args else {
        eprintln!("usage: plangraph batch-size INPUT.json");
        return 2;
    };
    let parsed = std::fs::read(path)
        .map_err(|e| e.to_string())
        .and_then(|b| serde_json::from_slice::<Input>(&b).map_err(|e| e.to_string()));
    match parsed {
        Ok(input) => {
            println!(
                "{}",
                serde_json::to_string(&plan(&input)).unwrap_or_default()
            );
            0
        }
        Err(error) => {
            eprintln!("batch-size: {error}");
            2
        }
    }
}
