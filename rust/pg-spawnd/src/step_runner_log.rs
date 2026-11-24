//! The stage sink of the step runner: the whole list on disk after every attempt, and the
//! landing journal row for the steps the keeper's stage table knows.

use crate::{landing_journal, step_runner::StageSink};
use pg_worker_core::batch_steps::{BatchSteps, ResultOf, Step, StepAttempt};
use serde_json::{json, Value};
use std::path::PathBuf;

pub struct StepLog {
    /// File holding the list as JSON; it is the input of `Runner::resume`.
    pub path: PathBuf,
    /// `OWNER/NAME` of the landing journal, or `None` to skip the journal.
    pub journal_repo: Option<String>,
    /// Head of the rows of steps that have none yet (claim, worktree, a failed write).
    pub base: String,
}

/// The keeper stage of a step, `None` for the steps its stage table has no name for.
fn keeper_stage(step: &Step) -> Option<&'static str> {
    Some(match step {
        Step::Write { .. } | Step::Fix { .. } => "writing",
        Step::FastChecks { .. } => "fast_checks",
        Step::Review { .. } => "review",
        Step::Push { .. } => "push",
        Step::CiClass { .. } => "ci",
        Step::Claim { .. } | Step::Worktree { .. } | Step::TrainJoin { .. } => return None,
    })
}

fn head<'a>(step: &'a Step, base: &'a str) -> &'a str {
    match step {
        Step::Write {
            result: ResultOf::Completed { output },
            ..
        } => &output.head,
        Step::Fix {
            result: ResultOf::Completed { output },
            ..
        } => &output.head,
        Step::FastChecks { head, .. }
        | Step::Review { head, .. }
        | Step::Fix { head, .. }
        | Step::Push { head, .. }
        | Step::CiClass { head, .. }
        | Step::TrainJoin { head, .. } => head,
        Step::Claim { .. } | Step::Worktree { .. } | Step::Write { .. } => base,
    }
}

/// The journal row of one attempt. The keeper keys a row by stage and attempt, so `attempt`
/// is the 1-based position of the row in the list, which is unique where a fix round repeats
/// the same stage.
pub fn journal_row(
    repo: &str,
    batch: &str,
    base: &str,
    ordinal: usize,
    row: &StepAttempt,
) -> Option<Value> {
    let stage = keeper_stage(&row.step)?;
    Some(json!({
        "action": "stage", "repo": repo, "batch_key": batch, "stage": stage,
        "head": head(&row.step, base), "attempt": ordinal,
        "started_at_ms": row.stage.started_at_ms, "ended_at_ms": row.stage.ended_at_ms,
    }))
}

impl StageSink for StepLog {
    fn record(&mut self, steps: &BatchSteps) {
        // Write beside the target and rename, so a crash never leaves half a list.
        let tmp = self.path.with_extension("tmp");
        let saved = serde_json::to_vec_pretty(steps)
            .map_err(|err| err.to_string())
            .and_then(|bytes| std::fs::write(&tmp, bytes).map_err(|err| err.to_string()))
            .and_then(|()| std::fs::rename(&tmp, &self.path).map_err(|err| err.to_string()));
        if let Err(err) = saved {
            eprintln!("step list not saved: {err}");
        }
        let (Some(repo), Some(last)) = (&self.journal_repo, steps.rows.last()) else {
            return;
        };
        if let Some(row) = journal_row(repo, &steps.batch_key, &self.base, steps.rows.len(), last) {
            if landing_journal::deliver(&row).is_err() {
                eprintln!("landing stage {} pending keeper delivery", row["stage"]);
            }
        }
    }
}
