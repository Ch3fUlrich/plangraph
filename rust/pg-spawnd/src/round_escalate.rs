//! Fix-round counting and escalation (rule 3.35, D-6731, D-6746). Every writer fix round of an
//! item is one admitted `implement` run, whatever started it (`CAUSES`), and carries the failing
//! checks seen when it started. `round_progress::admit` counts the failing checks that follow a
//! round together with its confirmed HIGH+MED findings; when `STALL_ROUNDS` rounds in a row
//! bring no new best, a replan record is queued (`round_replan::enqueue_escalation`; the
//! strongest planning leg of the route plans the slices) and the writer stays the same. Round
//! `MAX_ROUNDS + 1` is refused and the batch goes back to the keeper (the refusal's replan job).
use crate::{errors::ToolError, round_progress::refused};
use serde_json::{json, Value};
use std::collections::BTreeMap;

#[cfg(test)]
#[path = "round_escalate_tests.rs"]
mod tests;

/// What can start a writer fix round.
pub(crate) const CAUSES: [&str; 4] = ["review_finding", "red_ci", "rebase_main", "fast_check"];

/// The cause of the round the card starts; a card without one is a review finding.
pub(crate) fn cause(card: &Value) -> Result<&'static str, ToolError> {
    match card.get("round_cause").filter(|v| !v.is_null()) {
        None => Ok(CAUSES[0]),
        Some(v) => CAUSES
            .iter()
            .copied()
            .find(|c| v.as_str() == Some(*c))
            .ok_or_else(|| refused("round_cause_invalid")),
    }
}

/// The failing checks (red CI jobs, failed fast checks) the card reports at the start of its
/// round; None when the card does not say.
pub(crate) fn failing_checks(card: &Value) -> Result<Option<u64>, ToolError> {
    match card.get("failing_checks").filter(|v| !v.is_null()) {
        None => Ok(None),
        Some(v) => v
            .as_u64()
            .map(Some)
            .ok_or_else(|| refused("round_failing_checks_invalid")),
    }
}

/// The escalation level the history reached and the index of the first round run on it; the
/// stall window of a level starts there.
pub(crate) fn level(history: &[Value]) -> (u64, usize) {
    let level = history
        .iter()
        .filter_map(|r| r["round_escalation"].as_u64())
        .max()
        .unwrap_or(0);
    let since = history
        .iter()
        .position(|r| level > 0 && r["round_escalation"].as_u64() == Some(level))
        .unwrap_or(0);
    (level, since)
}

/// Per item rounds by cause, escalations and replans, escalations per day, and the items over
/// the ceiling, for the runs started in `[since, until)` (seconds).
pub(crate) fn report(records: &[Value], since: f64, until: f64) -> Value {
    #[derive(Default)]
    struct Item {
        rounds: u64,
        causes: BTreeMap<String, u64>,
        escalations: u64,
        replans: u64,
    }
    let (mut items, mut days) = (
        BTreeMap::<String, Item>::new(),
        BTreeMap::<String, u64>::new(),
    );
    for record in records.iter().filter(|r| r["role"] == "implement") {
        let (Some(item), Some(at)) = (
            record["item_key"].as_str(),
            record["started"]
                .as_f64()
                .or_else(|| record["ended"].as_f64()),
        ) else {
            continue;
        };
        if at < since || at >= until {
            continue;
        }
        let entry = items.entry(item.to_string()).or_default();
        if record["round_replan"] == "queued" || record["replan"] == "queued" {
            entry.replans += 1;
        }
        let voided = record["round_void"] == true && record["status"] != "completed";
        if !record["writer_round"].is_u64()
            || voided
            || matches!(record["status"].as_str(), Some("dry_run" | "refused"))
        {
            continue;
        }
        entry.rounds += 1;
        *entry
            .causes
            .entry(
                record["round_cause"]
                    .as_str()
                    .unwrap_or(CAUSES[0])
                    .to_string(),
            )
            .or_default() += 1;
        if record["round_escalation_reason"].is_string() {
            entry.escalations += 1;
            let day = chrono::DateTime::from_timestamp(at as i64, 0)
                .map(|t| t.format("%Y-%m-%d").to_string())
                .unwrap_or_default();
            *days.entry(day).or_default() += 1;
        }
    }
    let max = items.values().map(|i| i.rounds).max().unwrap_or(0);
    let over: Vec<_> = items
        .iter()
        .filter(|(_, i)| i.rounds > crate::round_progress::MAX_ROUNDS)
        .map(|(k, _)| k.clone())
        .collect();
    let items: BTreeMap<_, _> = items
        .into_iter()
        .map(|(k, i)| {
            let row = json!({"rounds":i.rounds, "by_cause":i.causes,
                "escalations":i.escalations, "replans":i.replans});
            (k, row)
        })
        .collect();
    json!({"items":items, "escalations_per_day":days, "max_rounds":max, "over_cap":over})
}
