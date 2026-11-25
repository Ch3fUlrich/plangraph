//! Fix-round counting and the replan gate of a stall (rule 3.35, D-6731, D-6746): every writer
//! fix round of a batch counts, whatever started it (`CAUSES`); `round_progress::admit` adds the
//! failing checks to the findings (`counts`) and `govern` holds a stalled round for its replan job.
use crate::{errors::ToolError, placement::Args, round_progress::refused};
use serde_json::{json, Value};
use std::path::Path;

#[cfg(test)]
#[path = "round_escalate_tests.rs"]
mod tests;

/// What can start a writer fix round.
pub(crate) const CAUSES: [&str; 4] = ["review_finding", "red_ci", "rebase_main", "fast_check"];

/// The first implementation of a batch: not a fix turn, so it takes no round number.
pub(crate) const INITIAL: &str = "initial";

/// The ledger key of a run record or card: its batch key, else its item key.
pub(crate) fn batch_key(value: &Value) -> Option<&str> {
    value["batch_key"]
        .as_str()
        .or_else(|| value["item_key"].as_str())
}

/// The stall of the rule on `counts`; misses count from round index `since` (the round a replan
/// resumed) and the half-of-round-1 check applies to the first level only.
pub(crate) fn stall_reason(counts: &[u64], since: usize) -> Option<&'static str> {
    // Round 1 has no earlier count to beat: it is a miss until a later round is strictly lower.
    let (mut best, mut misses) = (None, 0);
    for (index, &count) in counts.iter().enumerate() {
        match best {
            Some(b) if count < b => (best, misses) = (Some(count), 0),
            _ => {
                best = best.or(Some(count));
                misses += 1;
            }
        }
        if index < since {
            misses = 0;
        }
        if misses >= crate::round_progress::STALL_ROUNDS {
            return Some("no_progress");
        }
    }
    match (counts.first(), counts.last()) {
        (Some(&first), Some(&last))
            if since == 0
                && counts.len() as u64 >= crate::round_progress::STALL_ROUNDS
                && last * 2 > first =>
        {
            Some("slow_progress")
        }
        _ => None,
    }
}

/// The ledger key of the card (see `batch_key`); a malformed `batch_key` is refused.
pub(crate) fn ledger_key(card: &Value, item: &str) -> Result<String, ToolError> {
    match card.get("batch_key").filter(|v| !v.is_null()) {
        None => Ok(item.to_string()),
        Some(v) => v
            .as_str()
            .filter(|s| crate::records::valid_item(s))
            .map(String::from)
            .ok_or_else(|| refused("round_batch_key_invalid")),
    }
}

/// What the card starts: Err(fields) for the initial implementation (the first launch without a
/// `round_cause`), else Ok((cause, failing checks)) of a fix. Nothing starts while it runs.
pub(crate) fn start(
    state: &Path,
    card: &Value,
    batch: &str,
    history: &[Value],
) -> Result<Result<(&'static str, Option<u64>), Value>, ToolError> {
    let runs = crate::records::list_runs(state);
    let mut initials = runs.iter().filter(|r| {
        r["round_cause"] == INITIAL
            && batch_key(r) == Some(batch)
            && !matches!(r["status"].as_str(), Some("dry_run" | "refused"))
            // An attempt that did no work (voided) leaves the batch as it was: retry the initial run.
            && !(r["round_void"] == true && crate::runs::terminal(r) && r["status"] != "completed")
    });
    // A finished initial run leaves the numbered history empty but is not to be repeated.
    let seen = initials.clone().next().is_some();
    if initials.any(|r| !crate::runs::terminal(r)) {
        return Err(refused("round_in_flight"));
    }
    let named = card.get("round_cause").is_some_and(|v| !v.is_null());
    let first = history.is_empty() && !seen;
    let cause = match first && !named {
        true => INITIAL,
        false => cause(card)?,
    };
    if cause == INITIAL {
        return match first {
            true => Ok(Err(json!({"round_cause":cause, "batch_key":batch}))),
            false => Err(refused("round_initial_after_fix")),
        };
    }
    let checks = failing_checks(card)?;
    if cause != CAUSES[0] && checks.is_none() {
        return Err(refused("round_failing_checks_missing"));
    }
    Ok(Ok((cause, checks)))
}

/// `args` with `addendum` appended to its prompt, None when there is none.
pub(crate) fn with_addendum(args: &Args, addendum: &Option<String>) -> Option<Args> {
    let mut copy = args.clone();
    let prompt = args["prompt"].as_str().unwrap_or("");
    copy.insert(
        "prompt".into(),
        json!(format!("{prompt}\n\n{}", addendum.as_ref()?)),
    );
    Some(copy)
}

/// The cause of the round the card starts; a card without one is a review finding.
pub(crate) fn cause(card: &Value) -> Result<&'static str, ToolError> {
    match card.get("round_cause").filter(|v| !v.is_null()) {
        None => Ok(CAUSES[0]),
        Some(v) => CAUSES
            .iter()
            .copied()
            .chain([INITIAL])
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

/// The progress count of every round: its confirmed HIGH+MED plus the failing checks that follow
/// it (`checks` for the round about to start); an omitted count carries the last observed one.
pub(crate) fn counts(history: &[Value], summaries: &[Value], checks: Option<u64>) -> Vec<u64> {
    let mut observed = 0;
    (0..summaries.len())
        .map(|i| {
            observed = history
                .get(i + 1)
                .map_or(checks, |r| r["round_failing_checks"].as_u64())
                .or_else(|| history[i]["round_failing_checks"].as_u64())
                .unwrap_or(observed);
            summaries[i]["confirmed_blockers"].as_u64().unwrap() + observed
        })
        .collect()
}

/// A delivered replan the admitted round resumes with.
pub(crate) struct Resume {
    pub level: u64,
    pub reason: &'static str,
    pub addendum: Option<String>,
}

/// What the replan job of the next level asks of the admission. The saved job governs: a stall
/// queues it (never on a dry run) and every retry waits for its report, whatever check count it
/// carries; then the round resumes. A stall without a PR has no job and refuses.
pub(crate) fn govern(
    state: &Path,
    batch: &str,
    pr: &Value,
    level: u64,
    stall: Option<&'static str>,
    dry: bool,
) -> Result<Option<Resume>, ToolError> {
    let Some(pr) = pr.as_str() else {
        return stall.map_or(Ok(None), |reason| Err(refused(reason)));
    };
    let next = level + 1;
    let job = crate::round_escalation_job::sent(state, batch, pr, next);
    match (job, stall) {
        (None, None) => Ok(None),
        (Some(true), _) => Ok(Some(Resume {
            level: next,
            reason: stall.unwrap_or("replan"),
            addendum: crate::round_escalation_job::brief(state, batch, pr, next),
        })),
        (Some(false), _) => Err(refused("round_replan_pending")),
        (None, Some(_)) => {
            if !dry {
                crate::round_escalation_job::enqueue(state, batch, pr, next)
                    .map_err(|_| refused("round_replan_unavailable"))?;
            }
            Err(refused("round_replan_pending"))
        }
    }
}
