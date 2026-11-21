//! The main-freeze handler of the CI watcher (red-main design D3, D6): a fixed recovery table over the runs
//! the main state function names as blocking, a budget per run, one action per ref at a time, each action
//! reconciled by reading the run's attempts, and the alerts that go to routing with the party that is due.
use crate::land::main_state::{Answer, Blocking, Failure, State};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// Reconciliation deadline of a recovery request (seconds).
pub const RECONCILE_SECS: i64 = 10 * 60;
/// A red main without progress alerts after this long (seconds).
pub const NO_PROGRESS_SECS: i64 = 30 * 60;
/// A pending main alerts after T plus this (seconds).
pub const PENDING_EXTRA_SECS: i64 = 15 * 60;
/// `state_unknown` alerts after this long (seconds).
pub const UNKNOWN_SECS: i64 = 15 * 60;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Kind {
    RerunFailed,
    RerunAll,
    Cancel,
}

/// What is still allowed for one run. Persisted per run, never per attempt.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Budget {
    pub flake_rerun: bool,
    pub full_rerun: bool,
    /// 0 untouched, 1 cancelled and waiting for its rerun, 2 rerun requested.
    pub stall: u8,
    pub fix_task: bool,
}

/// A request that has not been reconciled yet.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Pending {
    pub run: u64,
    pub kind: Kind,
    pub attempts_before: u32,
    pub requested_at: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Alert {
    pub id: String,
    pub kind: String,
    /// The party that is due: routing, plan-order, host session or operator.
    pub due: String,
    pub detail: String,
    pub raised_at: i64,
}

#[derive(Default, Serialize, Deserialize)]
pub struct FreezeState {
    pub budgets: BTreeMap<u64, Budget>,
    pub pending: Option<Pending>,
    /// Requests that never showed a new attempt: recorded, never submitted again.
    pub unresolved: Vec<Pending>,
    pub alerts: BTreeMap<String, Alert>,
    pub red_since: Option<(String, i64)>,
    pub last_progress: i64,
    pub pending_since: Option<(String, i64)>,
    pub unknown_since: Option<i64>,
}

#[derive(Clone, Debug, Default)]
pub struct Diagnosis {
    pub tests: Vec<String>,
    /// The diagnosis of some failed job may be partial (capped log, job died early).
    pub incomplete: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Decision {
    Act(Kind),
    FixTask,
    /// The budget is spent and the run still blocks: the owner is routing.
    Spent,
    /// A run that cannot be rerun: no push run exists.
    NoRun,
    Wait,
}

fn rerun_conclusion(conclusion: &str) -> bool {
    matches!(conclusion, "cancelled" | "timed_out" | "startup_failure")
}

/// The recovery table. `latest_conclusion` is the conclusion of the blocking run's latest completed attempt.
pub fn decide(
    blocking: &Blocking,
    latest_conclusion: Option<&str>,
    diagnosis: &Diagnosis,
    flakes: &BTreeMap<String, String>,
    budget: &Budget,
) -> Decision {
    if blocking.run == 0 {
        return Decision::NoRun;
    }
    if blocking.stalled || budget.stall == 1 {
        return match budget.stall {
            0 => Decision::Act(Kind::Cancel),
            1 if !blocking.stalled => Decision::Act(Kind::RerunAll),
            _ => Decision::Spent,
        };
    }
    let workflow = blocking
        .failures
        .iter()
        .any(|f| matches!(f, Failure::Workflow { .. }));
    if workflow || latest_conclusion.is_some_and(rerun_conclusion) {
        return if budget.full_rerun {
            Decision::Spent
        } else {
            Decision::Act(Kind::RerunAll)
        };
    }
    let mapped = !diagnosis.tests.is_empty()
        && !diagnosis.incomplete
        && diagnosis.tests.iter().all(|t| flakes.contains_key(t));
    if mapped && !budget.flake_rerun {
        return Decision::Act(Kind::RerunFailed);
    }
    if budget.fix_task {
        return Decision::Wait;
    }
    Decision::FixTask
}

/// The effects of the handler; the Github adapter implements them.
pub trait Effects {
    /// The latest attempt number of the run and whether that attempt is completed.
    fn attempts(&mut self, run: u64) -> Result<(u32, bool), String>;
    fn diagnose(&mut self, run: u64, attempt: u32) -> Diagnosis;
    fn latest_conclusion(&mut self, run: u64) -> Option<String>;
    fn request(&mut self, run: u64, kind: Kind) -> Result<(), String>;
    fn fix_task(&mut self, run: u64, diagnosis: &Diagnosis) -> Result<(), String>;
}

fn raise(
    state: &mut FreezeState,
    kind: &str,
    due: &str,
    detail: String,
    id: String,
    now: i64,
) -> bool {
    if state.alerts.contains_key(&id) {
        return false;
    }
    state.alerts.insert(
        id.clone(),
        Alert {
            id,
            kind: kind.into(),
            due: due.into(),
            detail,
            raised_at: now,
        },
    );
    true
}

fn describe(b: &Blocking) -> String {
    format!(
        "run {} attempt {} failures {:?}",
        b.run, b.attempt, b.failures
    )
}

/// Progress tied to the blocking set: a blocking run's attempt that is queued or running and younger than T.
fn progress(answer: &Answer, blocking: &[Blocking]) -> bool {
    blocking.iter().any(|b| {
        answer.runs.iter().filter(|r| r.id == b.run).any(|r| {
            r.attempts
                .iter()
                .any(|a| a.status != "completed" && answer.at - a.started < answer.bound_t)
        })
    })
}

/// Refreshes the timers and raises the alerts of design D6 for this read.
pub fn refresh_alerts(
    state: &mut FreezeState,
    answer: &Answer,
    fix_task_recent: bool,
) -> Vec<String> {
    let now = answer.at;
    let mut out = Vec::new();
    let tip = answer.tip.clone();
    if !matches!(answer.state, State::Red { .. }) {
        state.red_since = None;
    }
    if !matches!(answer.state, State::Pending) {
        state.pending_since = None;
    }
    if !matches!(answer.state, State::StateUnknown { .. }) {
        state.unknown_since = None;
    }
    match &answer.state {
        State::Green => state.alerts.clear(),
        State::Red { blocking } => {
            if state.red_since.as_ref().map(|(t, _)| t) != Some(&tip) {
                state.red_since = Some((tip.clone(), now));
                state.last_progress = now;
            }
            if progress(answer, blocking) || fix_task_recent {
                state.last_progress = now;
            }
            let since = state.red_since.as_ref().map_or(now, |(_, s)| *s);
            if now - since.max(state.last_progress) >= NO_PROGRESS_SECS {
                let detail = blocking.iter().map(describe).collect::<Vec<_>>().join("; ");
                if raise(
                    state,
                    "main_red_no_progress",
                    "routing",
                    detail,
                    format!("main_red_no_progress:{tip}"),
                    now,
                ) {
                    out.push(format!("alert main_red_no_progress {tip}"));
                }
            }
        }
        State::Pending => {
            if state.pending_since.as_ref().map(|(t, _)| t) != Some(&tip) {
                state.pending_since = Some((tip.clone(), now));
            }
            let since = state.pending_since.as_ref().map_or(now, |(_, s)| *s);
            if now - since > answer.bound_t + PENDING_EXTRA_SECS
                && raise(
                    state,
                    "main_pending_aged",
                    "routing",
                    format!("tip {tip}"),
                    format!("main_pending_aged:{tip}"),
                    now,
                )
            {
                out.push(format!("alert main_pending_aged {tip}"));
            }
        }
        State::StateUnknown { failing_read } => {
            let since = *state.unknown_since.get_or_insert(now);
            if now - since >= UNKNOWN_SECS
                && raise(
                    state,
                    "main_state_unknown",
                    "host session",
                    failing_read.clone(),
                    format!("main_state_unknown:{since}"),
                    now,
                )
            {
                out.push("alert main_state_unknown".into());
            }
        }
        State::RefNotForwardOnly => {
            if raise(
                state,
                "main_ref_not_forward_only",
                "operator",
                "protection allows force push or deletion".into(),
                "main_ref_not_forward_only".into(),
                now,
            ) {
                out.push("alert main_ref_not_forward_only".into());
            }
        }
    }
    out
}

/// Reconciles the pending request: `Ok(true)` when the ref is free for the next action.
fn reconcile(
    state: &mut FreezeState,
    now: i64,
    fx: &mut impl Effects,
    out: &mut Vec<String>,
) -> Result<bool, String> {
    let Some(p) = state.pending.clone() else {
        return Ok(true);
    };
    let (attempt, completed) = fx.attempts(p.run)?;
    let seen = match p.kind {
        Kind::Cancel => completed,
        _ => attempt > p.attempts_before,
    };
    if seen {
        state.pending = None;
        out.push(format!("reconciled {:?} run {}", p.kind, p.run));
        return Ok(true);
    }
    if now - p.requested_at >= RECONCILE_SECS {
        out.push(format!("unresolved {:?} run {}", p.kind, p.run));
        raise(
            state,
            "recovery_unresolved",
            "routing",
            format!(
                "{:?} for run {} showed no new attempt in 10 minutes",
                p.kind, p.run
            ),
            format!(
                "recovery_unresolved:{}:{:?}:{}",
                p.run, p.kind, p.requested_at
            ),
            now,
        );
        state.unresolved.push(p);
        state.pending = None;
        return Ok(true);
    }
    Ok(false)
}

/// One pass of the handler. `save` persists the state before any request leaves.
pub fn step(
    state: &mut FreezeState,
    answer: &Answer,
    flakes: &BTreeMap<String, String>,
    fx: &mut impl Effects,
    fix_task_recent: bool,
    mut save: impl FnMut(&FreezeState) -> Result<(), String>,
) -> Result<Vec<String>, String> {
    let now = answer.at;
    let mut out = refresh_alerts(state, answer, fix_task_recent);
    let free = reconcile(state, now, fx, &mut out);
    save(state)?;
    if !free? {
        return Ok(out);
    }
    let State::Red { blocking } = &answer.state else {
        return Ok(out);
    };
    let mut ordered: Vec<&Blocking> = blocking.iter().collect();
    ordered.sort_by_key(|b| b.run);
    // A request whose reply was lost is never submitted a second time; its run is skipped so that another
    // blocking run on the ref can be recovered.
    let unresolved: BTreeSet<u64> = state.unresolved.iter().map(|p| p.run).collect();
    for b in ordered {
        if unresolved.contains(&b.run) {
            continue;
        }
        let budget = state.budgets.get(&b.run).cloned().unwrap_or_default();
        let conclusion = fx.latest_conclusion(b.run);
        let diagnosis = if b.run == 0 || b.stalled {
            Diagnosis::default()
        } else {
            fx.diagnose(b.run, b.attempt)
        };
        match decide(b, conclusion.as_deref(), &diagnosis, flakes, &budget) {
            Decision::Wait => continue,
            Decision::NoRun => {
                if raise(
                    state,
                    "main_red_no_push_run",
                    "routing",
                    "no push run exists for the tip".into(),
                    format!("none:{}", answer.tip),
                    now,
                ) {
                    out.push("alert no push run".into());
                }
            }
            Decision::Spent => {
                let id = format!("budget_spent:{}:{}", b.run, b.attempt);
                if raise(
                    state,
                    "recovery_budget_spent",
                    "routing",
                    describe(b),
                    id,
                    now,
                ) {
                    out.push(format!("budget spent: {}", describe(b)));
                }
            }
            Decision::FixTask => {
                state.budgets.entry(b.run).or_default().fix_task = true;
                save(state)?;
                fx.fix_task(b.run, &diagnosis)?;
                out.push(format!("fix task: run {}", b.run));
                return Ok(out);
            }
            Decision::Act(kind) => {
                let entry = state.budgets.entry(b.run).or_default();
                match kind {
                    Kind::RerunFailed => entry.flake_rerun = true,
                    Kind::RerunAll if entry.stall == 1 => entry.stall = 2,
                    Kind::RerunAll => entry.full_rerun = true,
                    Kind::Cancel => entry.stall = 1,
                }
                state.pending = Some(Pending {
                    run: b.run,
                    kind,
                    attempts_before: b.attempt,
                    requested_at: now,
                });
                // Reserve before requesting: a lost response must not cause another request.
                save(state)?;
                match fx.request(b.run, kind) {
                    Ok(()) => out.push(format!("requested {kind:?} run {}", b.run)),
                    Err(e) => {
                        state.pending = None;
                        raise(
                            state,
                            "recovery_refused",
                            "routing",
                            format!("GitHub refused {kind:?} for run {}: {e}", b.run),
                            format!("recovery_refused:{}:{kind:?}", b.run),
                            now,
                        );
                        out.push(format!("refused {kind:?} run {}", b.run));
                    }
                }
                save(state)?;
                return Ok(out);
            }
        }
    }
    save(state)?;
    Ok(out)
}
