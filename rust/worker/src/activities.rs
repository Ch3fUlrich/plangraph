//! Temporal activities and the wire types they exchange with the card workflow.
//!
//! Every activity here is a thin, side-effecting shell: the deterministic parts
//! live in `pg_worker_core` (probe, janitor, budget) and the blocking parts in
//! the sibling modules (attempt, mergelock, keeper). The activity body owns the
//! engine-specific concerns the workflow must not have: wall clock, jitter,
//! heartbeats and cancellation. The mapping helpers are factored out as plain
//! `pub(crate)` functions so they can be tested without a Temporal server.

use crate::attempt::{AttemptResult, LegSpec};
use crate::harness::HarnessRegistry;
use crate::keeper::{Alarm, AttemptReport, Claim, ExecId, Fence, Keeper, KeeperError};
use crate::mergelock::{self, MergeOutcome};
use crate::question::Question;
use pg_worker_core::budget;
use pg_worker_core::check;
use pg_worker_core::config::Config;
use pg_worker_core::confine;
use pg_worker_core::exit_class::Patterns;
use pg_worker_core::git::{GitError, check_rev, git, git_bytes};
use pg_worker_core::hash;
use pg_worker_core::janitor;
use pg_worker_core::packet::ParkPacket;
use pg_worker_core::pathjail;
use pg_worker_core::probe::{self, ProbeError, ProbeOutcome};
use pg_worker_core::redact;
use pg_worker_core::worktree::AttemptId;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use temporalio_macros::activities;
use temporalio_sdk::activities::{ActivityContext, ActivityError};
include!("activities_types.rs");
include!("activities_lifecycle.rs");
include!("activities_gate.rs");
include!("activities_merge.rs");
#[activities]
impl CardActivities {
    /// Run one attempt. The blocking work runs on a worker thread while this
    /// side heartbeats; a cancel flips a flag the attempt polls, and the task is
    /// awaited to completion so its child processes are reaped, never orphaned.
    ///
    /// The name override pins the Temporal activity type to `attempt`: the
    /// Python projector matches that exact string, and the SDK default
    /// (`CardActivities::attempt`) would not do.
    #[activity(name = "attempt")]
    pub async fn attempt(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: crate::card_input::ProjectorInput,
    ) -> Result<AttemptResult, ActivityError> {
        let heartbeat = self.deps.cfg.heartbeat;
        let deps = self.deps.clone();
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut cancelled = ctx.is_cancelled();
        if cancelled {
            cancel_flag.store(true, Ordering::SeqCst);
        }
        // The leg names its harness; one this worker never registered is a
        // contract defect for the planner, not a retry for the budget (the
        // attempt reports it, after creating its worktree).
        let harness = deps.harnesses.get(&input.leg.harness);
        let mut handle = {
            let cancel_flag = cancel_flag.clone();
            let input = input.clone();
            tokio::task::spawn_blocking(move || {
                let is_cancelled = || cancel_flag.load(Ordering::SeqCst);
                crate::card_admit::run_attempt(
                    harness.as_deref(),
                    &deps.cfg,
                    &deps.patterns,
                    &input,
                    &is_cancelled,
                )
            })
        };

        let mut beats: u32 = 0;
        let joined = loop {
            tokio::select! {
                res = &mut handle => break res,
                _ = tokio::time::sleep(heartbeat) => {
                    // A heartbeat error means the activity is cancelled/gone; keep looping so the blocking task is still joined and reaped.
                    if ctx.record_heartbeat(beats).await.is_err() {
                        cancelled = true;
                        cancel_flag.store(true, Ordering::SeqCst);
                    }
                    beats = beats.wrapping_add(1);
                    if ctx.is_cancelled() {
                        cancelled = true;
                        cancel_flag.store(true, Ordering::SeqCst);
                    }
                }
                // The cancel future fires once; the guard keeps a second poll
                // from spinning while the blocking task drains.
                _ = ctx.cancelled(), if !cancelled => {
                    cancelled = true;
                    cancel_flag.store(true, Ordering::SeqCst);
                }
            }
        };

        let result = joined.map_err(activity_error)?;
        if cancelled {
            return Err(ActivityError::cancelled());
        }
        result.map_err(crate::card_admit::attempt_error)
    }

    /// Sample the clock for the workflow's backoff decisions.
    #[activity]
    pub async fn tick(
        self: Arc<Self>,
        _ctx: ActivityContext,
        _input: (),
    ) -> Result<Tick, ActivityError> {
        Ok(tick_now())
    }

    /// Probe an attempt worktree before retrying it.
    #[activity]
    pub async fn probe(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: ProbeInput,
    ) -> Result<ProbeResult, ActivityError> {
        let cfg = self.deps.cfg.clone();
        tokio::task::spawn_blocking(move || do_probe(&cfg, &input))
            .await
            .map_err(activity_error)
    }

    /// Run a card's named gate checks in a worktree and report the outcome.
    ///
    /// Cancellation kills the child process group: a cancel (or a failed
    /// heartbeat) flips a flag the blocking checks poll, which kills the
    /// group; the task is always joined afterwards so the children are reaped,
    /// never orphaned. Time is one shared budget of the configured
    /// `start_to_close`: each check gets `min(its own timeout_s, what is left
    /// of the budget)`, and a check that would start after the budget is spent
    /// is reported as not run.
    #[activity]
    pub async fn gate(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: GateInput,
    ) -> Result<GateResult, ActivityError> {
        let heartbeat = self.deps.cfg.heartbeat;
        let cfg = self.deps.cfg.clone();
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut cancelled = ctx.is_cancelled();
        if cancelled {
            cancel_flag.store(true, Ordering::SeqCst);
        }
        let mut handle = {
            let cancel_flag = cancel_flag.clone();
            tokio::task::spawn_blocking(move || {
                let is_cancelled = || cancel_flag.load(Ordering::SeqCst);
                do_gate(&cfg, &input, &is_cancelled)
            })
        };

        let mut beats: u32 = 0;
        let joined = loop {
            tokio::select! {
                res = &mut handle => break res,
                _ = tokio::time::sleep(heartbeat) => {
                    // A heartbeat error means the activity is cancelled/gone; keep looping so the blocking task is still joined and reaped.
                    if ctx.record_heartbeat(beats).await.is_err() {
                        cancelled = true;
                        cancel_flag.store(true, Ordering::SeqCst);
                    }
                    beats = beats.wrapping_add(1);
                    if ctx.is_cancelled() {
                        cancelled = true;
                        cancel_flag.store(true, Ordering::SeqCst);
                    }
                }
                // The cancel future fires once; the guard keeps a second poll
                // from spinning while the blocking task drains.
                _ = ctx.cancelled(), if !cancelled => {
                    cancelled = true;
                    cancel_flag.store(true, Ordering::SeqCst);
                }
            }
        };

        let run = joined.map_err(activity_error)?;
        if cancelled {
            return Err(ActivityError::cancelled());
        }
        match run.map_err(gate_error)? {
            GateRun::Done(result) => Ok(result),
            GateRun::Cancelled => Err(ActivityError::cancelled()),
        }
    }

    /// Fast-forward the attempt branch into main under the merge lock.
    #[activity]
    pub async fn merge(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: MergeInput,
    ) -> Result<MergeResult, ActivityError> {
        let keeper = self.deps.keeper.clone();
        let cfg = self.deps.cfg.clone();
        tokio::task::spawn_blocking(move || do_merge(keeper.as_ref(), &cfg, &input))
            .await
            .map_err(activity_error)?
            .map_err(activity_error)
    }

    /// Record one attempt with the keeper.
    #[activity]
    pub async fn report(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: AttemptReport,
    ) -> Result<(), ActivityError> {
        let keeper = self.deps.keeper.clone();
        let exec = exec_of(&ctx, Some(input.n.max(1)), None);
        tokio::task::spawn_blocking(move || keeper.report_attempt(&input, &exec))
            .await
            .map_err(activity_error)?
            .map_err(activity_error)
    }

    /// Capture the card state the park decision will present
    /// (EXEC-OBSERVED-STATE, G3-1): the workflow runs this just before it
    /// decides to park and carries the answer in the `park` activity input, so
    /// a retry of a delayed park still shows the state the run observed at
    /// decision time.
    ///
    /// The only keeper *read* left in the worker. It runs off the tokio worker
    /// threads in `spawn_blocking` behind an explicit 5 s timeout (G3-4). A
    /// read failure -- an unreachable keeper, a slow one, an unreadable or
    /// oversized body (G3-5) -- is a *retryable* activity failure (D1b): the
    /// activity retry policy (5 attempts, 2 s initial) re-runs the read inside
    /// this one scheduled activity -- no new workflow commands, so replay is
    /// safe -- and only when those run out does the workflow degrade to its
    /// live-set fallback, logging the cause. `Ok(None)` -- the keeper holds no
    /// state -- is "observed nothing", as a run that could not read always was.
    // A short wire name, pinned by `gate_activity_name_and_payload_shapes_are_stable`
    // beside `attempt`; without the override the macro would emit
    // "CardActivities::observe" like `gate`/`probe`/`merge`.
    #[activity(name = "observe")]
    pub async fn observe(
        self: Arc<Self>,
        _ctx: ActivityContext,
        card: String,
    ) -> Result<Option<String>, ActivityError> {
        observe_read(self.deps.keeper.clone(), card).await
    }

    /// Park a card: the keeper wants a human to look at the packet.
    ///
    /// EXEC-OBSERVED-STATE (G3-1): the observation was captured when the
    /// workflow decided to park (its `observe` activity) and travels in the
    /// input's `expected_state` key. This activity builds its exec identity
    /// from its context plus that input and never re-reads the keeper, so a
    /// Temporal retry of a delayed park presents the same observation the
    /// keeper may now refuse instead of a fresh, tautological read of whatever
    /// the projector wrote meanwhile. Everything below the context binding is
    /// [`park_path`], which the tests run directly (V7-5).
    #[activity]
    pub async fn park(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: String,
    ) -> Result<(), ActivityError> {
        let exec = exec_of(&ctx, park_attempt_of(&input), expected_state_of(&input));
        park_path(self.deps.keeper.clone(), input, exec).await
    }

    /// Tell the keeper the card merged.
    #[activity]
    pub async fn complete(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: DoneInput,
    ) -> Result<(), ActivityError> {
        let keeper = self.deps.keeper.clone();
        let exec = exec_of(&ctx, input.attempt, input.expected_state.clone());
        tokio::task::spawn_blocking(move || keeper.complete(&input.card, &input.merged_sha, &exec))
            .await
            .map_err(activity_error)?
            .map_err(activity_error)
    }

    /// Raise an alarm for a condition only a human can clear.
    #[activity]
    pub async fn alarm(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: Alarm,
    ) -> Result<(), ActivityError> {
        let keeper = self.deps.keeper.clone();
        let exec = exec_of(&ctx, input.attempt, None);
        tokio::task::spawn_blocking(move || keeper.alarm(&input, &exec))
            .await
            .map_err(activity_error)?
            .map_err(activity_error)
    }

    /// Store the agent's question. `Some(id)` on success, `None` when the
    /// keeper refused it (401, 403, 409 or 422: retrying cannot help, the
    /// workflow parks). A transient failure (transport, 408, 429, 5xx) is
    /// retried here with a backoff, then becomes an activity error that the
    /// activity retry policy handles.
    #[activity]
    pub async fn ask_question(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: AskInput,
    ) -> Result<Option<i64>, ActivityError> {
        let keeper = self.deps.keeper.clone();
        // EXEC-OBSERVED-STATE / G3-3: an ask keeps the any-of live-set CAS the
        // ADR documents (an ask never moves the card), so the workflow's
        // recorded set is sent unchanged; nothing is re-read here (G3-1).
        let exec = exec_of(&ctx, input.attempt, input.expected_state.clone());
        let res = tokio::task::spawn_blocking(move || {
            retry_transient(&TRANSIENT_BACKOFF, || {
                keeper.ask_question(&input.card, &input.question, &exec)
            })
        })
        .await
        .map_err(activity_error)?;
        settle_refusal(res, "ask_question")
    }

    /// Ask the keeper to land the finished phase branch; a refusal (including
    /// KeeperError::Status(400)) is data, not an error.
    #[activity]
    pub async fn phase_finish(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: PhaseFinishInput,
    ) -> Result<crate::phase::PhaseFinish, ActivityError> {
        let keeper = self.deps.keeper.clone();
        let (key, branch) = (input.key.clone(), input.branch.clone());
        let (head, provenance) = (input.head.clone(), input.pre_patch_phase);
        let res = tokio::task::spawn_blocking(move || {
            do_phase_finish(&*keeper, &key, &branch, &head, provenance.as_deref())
        })
        .await
        .map_err(activity_error)?;
        match res {
            Ok(f) => Ok(f),
            Err(e) if finish_is_refusal(&e) => Ok(crate::phase::PhaseFinish {
                state: crate::phase::FINISH_REFUSED.to_string(),
                pr: None,
                detail: format!("keeper refused phase_finish: {e}"),
            }),
            Err(e) => Err(activity_error(e)),
        }
    }

    /// Re-read the phase plan so overlap-deferred cards can become startable.
    #[activity]
    pub async fn phase_plan(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: PhasePlanInput,
    ) -> Result<crate::phase::PhasePlan, ActivityError> {
        let keeper = self.deps.keeper.clone();
        let key = input.key.clone();
        let res = tokio::task::spawn_blocking(move || do_phase_plan(&*keeper, &key))
            .await
            .map_err(activity_error)?;
        match res {
            Ok(plan) => Ok(plan),
            Err(e) if classify(&e) == Failure::Refused => Ok(crate::phase::PhasePlan {
                phase: input.key,
                ready: false,
                blocked: vec![format!("keeper refused phase_plan: {e}")],
                cards: vec![],
            }),
            Err(e) => Err(activity_error(e)),
        }
    }

    /// Claim one card for this worker before starting its child workflow.
    #[activity]
    pub async fn claim_card(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: ClaimCardInput,
    ) -> Result<Option<Claim>, ActivityError> {
        let keeper = self.deps.keeper.clone();
        let key = input.key;
        let cfg = self.deps.cfg.clone();
        tokio::task::spawn_blocking(move || admitted_claim(&*keeper, &cfg, &key))
            .await
            .map_err(activity_error)?
    }

    /// Create the phase branch once; a racing creator simply wins the CAS.
    #[activity]
    pub async fn ensure_phase_branch(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: PhaseBranchInput,
    ) -> Result<PhaseBranchResult, ActivityError> {
        tokio::task::spawn_blocking(move || do_ensure_phase_branch(&input))
            .await
            .map_err(activity_error)?
            .map_err(phase_branch_error)
    }

    /// Record `question_timeout` for a question whose default was taken.
    /// `true` when recorded; `false` on any failure that outlasts the transient
    /// retries here or is a refusal (for example the worker token lacks the
    /// router or system role): it is logged and the workflow raises an alarm
    /// but still continues with the default. This activity never errors, so a
    /// keeper outage cannot fail the card.
    #[activity]
    pub async fn record_question_timeout(
        self: Arc<Self>,
        _ctx: ActivityContext,
        question_id: i64,
    ) -> Result<bool, ActivityError> {
        let keeper = self.deps.keeper.clone();
        let res = tokio::task::spawn_blocking(move || {
            retry_transient(&TRANSIENT_BACKOFF, || {
                keeper.record_question_timeout(question_id)
            })
        })
        .await;
        // A keeper error and a dead blocking task are the same thing here:
        // not recorded. Never an activity error (that would be retried and then
        // fail the card).
        match res {
            Ok(Ok(())) => Ok(true),
            Ok(Err(e)) => {
                eprintln!("record_question_timeout not recorded: {e}");
                Ok(false)
            }
            Err(e) => {
                eprintln!("record_question_timeout task failed: {e}");
                Ok(false)
            }
        }
    }

    /// Sweep dead attempts and report how many were removed.
    #[activity]
    pub async fn janitor(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: JanitorInput,
    ) -> Result<u32, ActivityError> {
        let cfg = self.deps.cfg.clone();
        tokio::task::spawn_blocking(move || do_janitor(&cfg, &input))
            .await
            .map_err(activity_error)?
            .map_err(activity_error)
    }
}

#[cfg(all(test, unix))]
mod tests {
    include!("activities_regression_1.rs");
    include!("activities_regression_2.rs");
    include!("activities_regression_3.rs");
    include!("activities_regression_4.rs");
    include!("activities_regression_5.rs");
    include!("activities_regression_6.rs");
}
