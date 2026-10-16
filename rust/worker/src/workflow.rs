//! The `card` workflow: one workflow per card, one pass per attempt.
//!
//! Everything here must be deterministic on replay, so the clock and jitter
//! arrive only through the `tick` activity, the retry policy is the pure
//! `pg_worker_core::budget::next_action`, and the history is a plain `Vec`.
//! A pass runs one attempt on a fresh worktree, records it, then either merges
//! an `ok` result or asks the budget for the next move: retry, re-route, wait,
//! promote, upgrade the context class, quarantine or park.

use crate::activities::AskInput;
use crate::activities::{
    CardActivities, CardInput, CardOutput, ConfineSpec, DoneInput, JanitorInput, MergeInput,
    MergeResult, ProbeInput, ProbeResult, Tick,
};
use crate::attempt::{AttemptInput, AttemptResult, LegSpec, to_budget_attempt};
use crate::keeper::{Alarm, AttemptReport, Claim};
use crate::question::{self, OnTimer, Question};
use pg_worker_core::budget::{self, Action, Attempt, ContextClass, Leg};
use pg_worker_core::config::{Config, Tier};
use pg_worker_core::exit_class::ExitClass;
use pg_worker_core::packet::ParkPacket;
use serde::{Deserialize, Serialize};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use temporalio_common::RetryPolicy;
use temporalio_macros::{workflow, workflow_methods};
use temporalio_sdk::{
    ActivityOptions, SyncWorkflowContext, TimeoutType, WorkflowCancellationToken, WorkflowContext,
    WorkflowResult, WorkflowTermination,
};

/// Alarm reason when the total waited time reaches `Config::wait_cap`.
pub const WAIT_CAP_PARK_REASON: &str = "wait cap reached";

/// Alarm reason when a run of `probe_fail` attempts is abandoned.
const PROBE_FAIL_PARK_REASON: &str = "probe fail cap reached";

/// Consecutive `probe_fail` attempts tolerated before parking (R2 carry-over).
const PROBE_FAIL_CAP: usize = 5;

/// Durable pause before asking again when the keeper would not grant the merge lock.
const MERGE_RETRY_WAIT: Duration = Duration::from_secs(15);

/// Merge-lock retries before the card parks.
const MERGE_RETRY_CAP: u32 = 20;

/// Alarm reason when the keeper merge lock stayed unavailable.
const MERGE_LOCK_PARK_REASON: &str = "merge lock unavailable";

/// Alarm reason when a question stayed unanswered until the wait cap.
const QUESTION_PARK_REASON: &str = "question unanswered";

/// Alarm reason when the keeper would not store the agent's question.
const QUESTION_REFUSED_REASON: &str = "question not recorded";

/// Alarm reason when the default was taken but the keeper refused to record it.
const QUESTION_TIMEOUT_REFUSED_REASON: &str = "question_timeout not recorded";

/// The `answer` signal, exactly as the inbox sends it
/// (`src/plangraph/inbox/temporal_signal.py`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerSignal {
    pub question_id: i64,
    #[serde(default)]
    pub key: String,
    pub text: String,
    #[serde(default)]
    pub source: String,
}

/// How a question ended.
enum QuestionStep {
    /// The text the next attempt gets in its prompt.
    Answer(String),
    /// The card is over (parked or cancelled) and the janitor has run.
    Finished(CardOutput),
}

/// What the merge step produced.
#[derive(Debug)]
enum MergeStep {
    /// Main now points at this attempt; the value is the merged sha.
    Merged(String),
    /// Main moved or the attempt would not fast-forward; the value is the new base.
    Moved(String),
    /// The merge itself said no (post-rebase check, confinement, stale fence);
    /// the class goes to the policy, the text is the check tail.
    Failed(ExitClass, String),
    /// The keeper would not grant the merge lock; nothing was merged.
    RetryLater,
}

#[workflow]
#[derive(Default)]
pub struct Card {
    /// Set by the `cancel` signal; raced against every durable timer.
    cancel_requested: bool,
    /// Token handed to `attempt`; the `cancel` signal fires it.
    op: Option<WorkflowCancellationToken>,
    /// Every `answer` signal received, in arrival order.
    answers: Vec<AnswerSignal>,
}

impl Card {
    /// The human answer to question `id`, if one arrived. A `question_timeout`
    /// signal is not an answer: the workflow takes its own default.
    fn answer_text(&self, id: i64) -> Option<String> {
        self.answers
            .iter()
            .find(|a| a.question_id == id && a.source != "question_timeout")
            .map(|a| a.text.clone())
    }
}

#[workflow_methods]
impl Card {
    #[run]
    pub async fn run(
        ctx: &mut WorkflowContext<Self>,
        input: CardInput,
    ) -> WorkflowResult<CardOutput> {
        // A constant: the policy and the activity options must not vary per run.
        let cfg = Config::default();
        // A child of the workflow token, so native cancel and the signal both reach
        // the activity.
        let op = ctx.cancellation_token().child_token();
        ctx.state_mut(|s| s.op = Some(op.clone()));
        // A signal can be applied before this body runs, when `op` was still empty.
        if ctx.state(|s| s.cancel_requested) {
            op.cancel_with_reason("cancel before start".to_string());
        }

        // Local state, rebuilt exactly on replay.
        let mut history: Vec<Attempt> = Vec::new();
        let mut base_sha = input.claim.base_sha.clone();
        let mut n: u32 = 0;
        let mut waits: u32 = 0;
        let mut waited = Duration::ZERO;
        let mut context = ContextClass::K128;
        let mut limited: Vec<String> = Vec::new();
        let mut leg: LegSpec = input.legs[0].clone();
        // Answers to the agent's questions; each rides in the next prompt.
        let mut answers: Vec<String> = Vec::new();

        loop {
            // A cancel that landed between attempts must not start another one.
            if ctx.state(|s| s.cancel_requested) {
                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                return Ok(cancelled(n));
            }
            n += 1;

            // Time and jitter enter the workflow only here.
            let tick: Tick = ctx
                .execute_activity(CardActivities::tick, (), activity_options(&cfg))
                .await?;
            let now = UNIX_EPOCH + Duration::from_millis(tick.unix_ms);

            // A cancel that landed while `tick` ran would hit an `op` no activity is
            // listening to yet, so it is checked again before the attempt is scheduled.
            if ctx.state(|s| s.cancel_requested) {
                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                return Ok(cancelled(n - 1));
            }

            // The budget, not Temporal, decides retries: one attempt per activity run.
            let retry = RetryPolicy::builder()
                .initial_interval(Duration::from_secs(1))
                .maximum_attempts(1)
                .build();
            let opts = ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
                .heartbeat_timeout(cfg.heartbeat_timeout)
                .retry_policy(retry)
                .cancellation_token(op.clone())
                .build();
            let attempt_input = AttemptInput {
                claim: Claim {
                    base_sha: base_sha.clone(),
                    ..input.claim.clone()
                },
                n,
                leg: leg.clone(),
                workroot: input.workroot.clone(),
                class: context_class_name(context).to_string(),
                answers: answers.clone(),
            };

            let result: AttemptResult = match ctx
                .execute_activity(CardActivities::attempt, attempt_input, opts)
                .await
            {
                Ok(r) => r,
                Err(e) if e.as_cancelled().is_some() => {
                    // A driven cancellation ends the run; a signal ends the card.
                    if ctx.cancellation_token().is_cancelled() {
                        // Reclaim the attempt's worktree before the run ends; the
                        // janitor is scheduled detached so it survives this cancel.
                        run_janitor(ctx, &cfg, &input, &base_sha).await?;
                        return Err(WorkflowTermination::cancelled());
                    }
                    run_janitor(ctx, &cfg, &input, &base_sha).await?;
                    return Ok(cancelled(n));
                }
                Err(e) => {
                    // The worker never returned a result. A heartbeat timeout means
                    // it is hung (a stall); a start-to-close or schedule-to-close
                    // timeout means it ran past its budget (a timeout); any other
                    // timeout kind stays a stall; a non-timeout is a crash. What
                    // follows is the budget's call.
                    let exit_class = match e.as_timeout().map(|t| t.timeout_type()) {
                        Some(TimeoutType::Heartbeat) => ExitClass::Stall,
                        Some(TimeoutType::StartToClose) | Some(TimeoutType::ScheduleToClose) => {
                            ExitClass::Timeout
                        }
                        Some(_) => ExitClass::Stall,
                        None => ExitClass::WorkerCrash,
                    };
                    AttemptResult {
                        exit_class,
                        diff_hash: String::new(),
                        check_hash: String::new(),
                        check_tail: String::new(),
                        context_overflow: false,
                        branch: pg_worker_core::worktree::AttemptId::new(
                            input.claim.card.as_str(),
                            n,
                        )
                        .branch(),
                        head: String::new(),
                        output_tail: String::new(),
                        // The projector records a missing field as absent; a
                        // synthetic attempt has no provider metadata of its own.
                        harness: None,
                        model: None,
                        combo: None,
                        class: None,
                        started_at: None,
                        ended_at: None,
                        tokens_in: None,
                        tokens_out: None,
                        cost: None,
                        log_ref: None,
                        question: None,
                    }
                }
            };

            // `run_attempt` reports a cancelled run as an `ok` result, so the signal
            // is re-checked here; native cancellation is honoured first.
            if ctx.cancellation_token().is_cancelled() {
                // Reclaim the attempt's worktree before the run ends; the janitor
                // is scheduled detached so it survives this cancel.
                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                return Err(WorkflowTermination::cancelled());
            }
            if ctx.state(|s| s.cancel_requested) {
                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                return Ok(cancelled(n));
            }

            // Hashes and tail of this attempt, reused by the synthetic attempts
            // (worker crash, rebase conflict, probe failure) that have none of their own.
            let diff_hash = result.diff_hash.clone();
            let check_hash = result.check_hash.clone();
            let mut check_tail = result.check_tail.clone();

            let report = AttemptReport {
                card: input.claim.card.clone(),
                n,
                leg: leg.id.clone(),
                exit_class: result.exit_class,
                diff_hash: result.diff_hash.clone(),
                check_tail: result.check_tail.clone(),
            };
            ctx.execute_activity(CardActivities::report, report, activity_options(&cfg))
                .await?;

            // A question is not a failure: it takes no retry budget, so the
            // attempt stays out of the history. The next attempt reruns the
            // same leg with the answer in its prompt.
            if result.exit_class == ExitClass::NeedsInput
                && let Some(q) = &result.question
            {
                match question_step(ctx, &cfg, &input, &base_sha, leg.tier, q, n).await? {
                    QuestionStep::Answer(text) => {
                        answers.push(text);
                        continue;
                    }
                    QuestionStep::Finished(out) => return Ok(out),
                }
            }
            history.push(to_budget_attempt(&result, &leg));

            let mut probed = false;
            let mut merge_retries: u32 = 0;
            let mut branch_to_merge: Option<String> = if result.exit_class == ExitClass::Ok {
                Some(result.branch.clone())
            } else {
                None
            };

            // Inner loop: a merge can move the base and hand a synthetic attempt back
            // to the policy without starting a new worktree.
            loop {
                // Step 4: merge an `ok` (or probed already-done) attempt now.
                if let Some(branch) = branch_to_merge.take() {
                    match merge_step(ctx, &cfg, &input, &base_sha, &branch, n).await? {
                        MergeStep::Merged(sha) => {
                            ctx.execute_activity(
                                CardActivities::complete,
                                DoneInput {
                                    card: input.claim.card.clone(),
                                    merged_sha: sha.clone(),
                                },
                                activity_options(&cfg),
                            )
                            .await?;
                            run_janitor(ctx, &cfg, &input, &base_sha).await?;
                            return Ok(finish("merged", Some(sha), n));
                        }
                        MergeStep::RetryLater => {
                            merge_retries += 1;
                            if merge_retries > MERGE_RETRY_CAP {
                                let packet = packet_for(
                                    &input.claim.card,
                                    leg.tier,
                                    ExitClass::MergeRefused,
                                    MERGE_LOCK_PARK_REASON,
                                );
                                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                                park_and_alarm(
                                    ctx,
                                    &cfg,
                                    &packet,
                                    MERGE_LOCK_PARK_REASON.to_string(),
                                )
                                .await?;
                                return Ok(parked(n));
                            }
                            if wait_for(ctx, MERGE_RETRY_WAIT).await? {
                                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                                return Ok(cancelled(n));
                            }
                            branch_to_merge = Some(branch);
                            continue;
                        }
                        MergeStep::Failed(class, tail) => {
                            // The branch was judged under the lock; probing it again
                            // would only find the same commits and loop back here.
                            probed = true;
                            check_tail = tail;
                            history.push(Attempt {
                                exit_class: class,
                                leg: leg.id.clone(),
                                tier: leg.tier,
                                diff_hash: diff_hash.clone(),
                                check_hash: check_hash.clone(),
                                context_overflow: false,
                            });
                        }
                        MergeStep::Moved(main_head) => {
                            // The worker already tried to rebase this attempt onto the
                            // moved main and could not (or had no worktree to try in).
                            // Take the new head as the base and let the policy retry
                            // from a fresh worktree; probing the branch again would
                            // only re-merge the same commits and count the conflict twice.
                            probed = true;
                            base_sha = main_head;
                            history.push(Attempt {
                                exit_class: ExitClass::RebaseConflict,
                                leg: leg.id.clone(),
                                tier: leg.tier,
                                diff_hash: diff_hash.clone(),
                                check_hash: check_hash.clone(),
                                context_overflow: false,
                            });
                        }
                    }
                }

                // Step 5: probe a non-ok attempt exactly once before the policy.
                if !probed {
                    probed = true;
                    let probe: ProbeResult = ctx
                        .execute_activity(
                            CardActivities::probe,
                            ProbeInput {
                                repo: input.claim.repo.clone(),
                                workroot: input.workroot.clone(),
                                card: input.claim.card.clone(),
                                n,
                                base_sha: base_sha.clone(),
                                check_cmd: input.claim.check_cmd.clone(),
                            },
                            activity_options(&cfg),
                        )
                        .await?;
                    match probe.outcome.as_str() {
                        "already_done" => {
                            branch_to_merge = Some(probe.branch);
                            continue;
                        }
                        "error" => history.push(Attempt {
                            exit_class: ExitClass::ProbeFail,
                            leg: leg.id.clone(),
                            tier: leg.tier,
                            diff_hash: diff_hash.clone(),
                            check_hash: check_hash.clone(),
                            context_overflow: false,
                        }),
                        _ => {}
                    }
                }

                // Bound the probe-fail backoff; the pure policy never parks on it.
                if probe_fail_streak(&history) >= PROBE_FAIL_CAP {
                    let packet = packet_for(
                        &input.claim.card,
                        leg.tier,
                        ExitClass::ProbeFail,
                        &check_tail,
                    );
                    run_janitor(ctx, &cfg, &input, &base_sha).await?;
                    park_and_alarm(ctx, &cfg, &packet, PROBE_FAIL_PARK_REASON.to_string()).await?;
                    return Ok(parked(n));
                }

                // Step 6: the pure policy decides the next move.
                let legs = legs_for(&input, &limited);
                let action = budget::next_action(
                    &cfg,
                    &budget::Input {
                        card: &input.claim.card,
                        history: &history,
                        legs: &legs,
                        context,
                        waits,
                        now,
                        jitter: tick.jitter,
                        check_tail: &check_tail,
                    },
                );

                match action {
                    Action::Done => {
                        branch_to_merge = Some(result.branch.clone());
                        continue;
                    }
                    Action::Retry => break,
                    Action::Reroute { leg: id } => {
                        match input.legs.iter().find(|l| l.id == id) {
                            Some(l) => leg = l.clone(),
                            None => {
                                let packet =
                                    packet_from_last(&input.claim.card, &history, &check_tail);
                                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                                park_and_alarm(
                                    ctx,
                                    &cfg,
                                    &packet,
                                    exit_name(packet.exit_class).to_string(),
                                )
                                .await?;
                                return Ok(parked(n));
                            }
                        }
                        break;
                    }
                    Action::Promote { tier } => {
                        match input.legs.iter().find(|l| l.tier == tier) {
                            Some(l) => leg = l.clone(),
                            None => {
                                let packet =
                                    packet_from_last(&input.claim.card, &history, &check_tail);
                                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                                park_and_alarm(
                                    ctx,
                                    &cfg,
                                    &packet,
                                    exit_name(packet.exit_class).to_string(),
                                )
                                .await?;
                                return Ok(parked(n));
                            }
                        }
                        break;
                    }
                    Action::UpgradeClass { class } => {
                        context = class;
                        break;
                    }
                    Action::Wait { until } => {
                        // Every leg of the tier is limited: wait, bounded by the cap.
                        limited.push(leg.id.clone());
                        let d = wait_duration(until, now);
                        waited = waited.saturating_add(d);
                        if waited >= cfg.wait_cap {
                            let packet = packet_from_last(&input.claim.card, &history, &check_tail);
                            run_janitor(ctx, &cfg, &input, &base_sha).await?;
                            park_and_alarm(ctx, &cfg, &packet, WAIT_CAP_PARK_REASON.to_string())
                                .await?;
                            return Ok(parked(n));
                        }
                        waits += 1;
                        if wait_for(ctx, d).await? {
                            run_janitor(ctx, &cfg, &input, &base_sha).await?;
                            return Ok(cancelled(n));
                        }
                        limited.clear();
                        break;
                    }
                    Action::Backoff { delay } => {
                        if wait_for(ctx, delay).await? {
                            run_janitor(ctx, &cfg, &input, &base_sha).await?;
                            return Ok(cancelled(n));
                        }
                        break;
                    }
                    Action::Quarantine => {
                        let packet = packet_from_last(&input.claim.card, &history, &check_tail);
                        run_janitor(ctx, &cfg, &input, &base_sha).await?;
                        park_and_alarm(
                            ctx,
                            &cfg,
                            &packet,
                            exit_name(packet.exit_class).to_string(),
                        )
                        .await?;
                        return Ok(finish("quarantined", None, n));
                    }
                    Action::ToPlanner => {
                        let packet = packet_from_last(&input.claim.card, &history, &check_tail);
                        run_janitor(ctx, &cfg, &input, &base_sha).await?;
                        park_and_alarm(
                            ctx,
                            &cfg,
                            &packet,
                            exit_name(packet.exit_class).to_string(),
                        )
                        .await?;
                        return Ok(finish("planner", None, n));
                    }
                    Action::Park { packet } => {
                        run_janitor(ctx, &cfg, &input, &base_sha).await?;
                        park_and_alarm(
                            ctx,
                            &cfg,
                            &packet,
                            exit_name(packet.exit_class).to_string(),
                        )
                        .await?;
                        return Ok(finish("parked", None, n));
                    }
                }
            }
        }
    }

    /// The inbox answer to a question the card asked.
    #[signal]
    pub fn answer(&mut self, _ctx: &mut SyncWorkflowContext<Self>, a: AnswerSignal) {
        self.answers.push(a);
    }

    #[signal]
    pub fn cancel(&mut self, _ctx: &mut SyncWorkflowContext<Self>, reason: String) {
        self.cancel_requested = true;
        if let Some(op) = &self.op {
            op.cancel_with_reason(reason);
        }
    }
}

/// The terminal outcome for a finished card, named for the projector.
///
/// docs/projector.md contract v1 gives the workflow result only
/// `done | parked | failed`; the keeper's finer word rides along in `detail`.
/// `cancelled` maps to `parked` because the projector never sets `cancelled` -
/// that word belongs to the keeper `cancel` verb, not to a workflow result.
pub fn finish(detail: &str, merged_sha: Option<String>, attempts: u32) -> CardOutput {
    let outcome = match detail {
        "merged" => "done",
        "parked" | "planner" | "cancelled" => "parked",
        "quarantined" => "failed",
        // An unknown detail is not evidence of success, so it fails closed.
        _ => "failed",
    };
    CardOutput {
        outcome: outcome.to_string(),
        detail: detail.to_string(),
        merged_sha,
        attempts,
    }
}

/// A terminal outcome with no merged sha, for the park family.
fn parked(attempts: u32) -> CardOutput {
    finish("parked", None, attempts)
}

/// A terminal outcome for a signal-cancelled card.
fn cancelled(attempts: u32) -> CardOutput {
    finish("cancelled", None, attempts)
}

/// The options every auxiliary activity gets: bounded retries for idempotent
/// auxiliary activities (a failure is a workflow signal, not something to
/// replay forever).
fn activity_options(cfg: &Config) -> ActivityOptions {
    let retry = RetryPolicy::builder()
        .initial_interval(Duration::from_secs(2))
        .maximum_attempts(5)
        .build();
    ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
        .retry_policy(retry)
        .build()
}

/// The options the `merge` activity gets: a single attempt, because a retried
/// merge after a successful ref update would falsely report `base_moved`.
fn once_options(cfg: &Config) -> ActivityOptions {
    let retry = RetryPolicy::builder()
        .initial_interval(Duration::from_secs(2))
        .maximum_attempts(1)
        .build();
    ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
        .retry_policy(retry)
        .build()
}

/// Wait `d` durably, unless the `cancel` signal wins first (`true`).
///
/// The signal cancels the attempt's child token, so a wait has to race it too,
/// or a card could sit in a 24 h timer after the operator cancelled it.
async fn wait_for(ctx: &WorkflowContext<Card>, d: Duration) -> WorkflowResult<bool> {
    let cancelled = temporalio_sdk::workflows::select! {
        r = ctx.wait_condition(|s: &Card| s.cancel_requested) => { r?; true }
        _ = ctx.timer(d) => false,
    };
    Ok(cancelled)
}

/// Ask the keeper `q`, then wait for the answer signal, a cancel, or the timer.
///
/// Waiting is durable workflow time, so a replay never sleeps. The timer is the
/// question deadline when a default may be taken, else the wait cap.
async fn question_step(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    input: &CardInput,
    base_sha: &str,
    tier: Tier,
    q: &Question,
    n: u32,
) -> WorkflowResult<QuestionStep> {
    let card = &input.claim.card;
    let q = q.normalized();
    let cap = input
        .question_wait_cap_s
        .map_or(cfg.wait_cap, Duration::from_secs);
    let plan = question::plan_wait(&q, cap);

    let asked: Option<i64> = ctx
        .execute_activity(
            CardActivities::ask_question,
            AskInput {
                card: card.clone(),
                question: q.clone(),
            },
            activity_options(cfg),
        )
        .await?;
    let Some(qid) = asked else {
        return park_question(
            ctx,
            cfg,
            input,
            base_sha,
            tier,
            &q,
            QUESTION_REFUSED_REASON,
            n,
        )
        .await;
    };

    enum Woke {
        Answer,
        Cancel,
        Timer,
    }
    let woke = temporalio_sdk::workflows::select! {
        r = ctx.wait_condition(move |s: &Card| s.answer_text(qid).is_some()) => { r?; Woke::Answer }
        r = ctx.wait_condition(|s: &Card| s.cancel_requested) => { r?; Woke::Cancel }
        _ = ctx.timer(plan.wait) => Woke::Timer,
    };
    match woke {
        Woke::Answer => {
            let text = ctx
                .state(|s| s.answer_text(qid))
                .expect("the wait ended on an answer");
            Ok(QuestionStep::Answer(text))
        }
        Woke::Cancel => {
            run_janitor(ctx, cfg, input, base_sha).await?;
            Ok(QuestionStep::Finished(cancelled(n)))
        }
        Woke::Timer => match plan.on_timer {
            OnTimer::UseDefault(text) => {
                let recorded: bool = ctx
                    .execute_activity(
                        CardActivities::record_question_timeout,
                        qid,
                        activity_options(cfg),
                    )
                    .await?;
                if !recorded {
                    // The default is still taken: the question window is over.
                    ctx.execute_activity(
                        CardActivities::alarm,
                        Alarm {
                            card: card.clone(),
                            reason: QUESTION_TIMEOUT_REFUSED_REASON.to_string(),
                        },
                        activity_options(cfg),
                    )
                    .await?;
                }
                Ok(QuestionStep::Answer(text))
            }
            OnTimer::Park => {
                park_question(ctx, cfg, input, base_sha, tier, &q, QUESTION_PARK_REASON, n).await
            }
        },
    }
}

/// End a card whose question cannot be answered: sweep, park and alarm.
#[allow(clippy::too_many_arguments)]
async fn park_question(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    input: &CardInput,
    base_sha: &str,
    tier: Tier,
    q: &Question,
    reason: &str,
    n: u32,
) -> WorkflowResult<QuestionStep> {
    let packet = packet_for(&input.claim.card, tier, ExitClass::NeedsInput, &q.text);
    run_janitor(ctx, cfg, input, base_sha).await?;
    park_and_alarm(ctx, cfg, &packet, reason.to_string()).await?;
    Ok(QuestionStep::Finished(parked(n)))
}

/// Merge `branch` into main, mapping the outcome to a value the run loop can act on.
async fn merge_step(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    input: &CardInput,
    base_sha: &str,
    branch: &str,
    n: u32,
) -> WorkflowResult<MergeStep> {
    let r: MergeResult = ctx
        .execute_activity(
            CardActivities::merge,
            MergeInput {
                repo: input.claim.repo.clone(),
                main_branch: input.main_branch.clone(),
                branch: branch.to_string(),
                base_sha: base_sha.to_string(),
                fence_token: None,
                worktree: format!("{}/{}-{n}", input.workroot, input.claim.card),
                check_cmd: Some(input.claim.check_cmd.clone()),
                confine: Some(ConfineSpec {
                    allow: input.claim.allow.clone(),
                    forbid: input.claim.forbid.clone(),
                }),
            },
            once_options(cfg),
        )
        .await?;
    Ok(match r.outcome.as_str() {
        "merged" => MergeStep::Merged(r.sha.unwrap_or(r.main_head)),
        "retry_later" => MergeStep::RetryLater,
        "check_fail" => MergeStep::Failed(ExitClass::CheckFail, r.detail),
        "contract_defect" => MergeStep::Failed(ExitClass::ContractDefect, r.detail),
        "merge_refused" => MergeStep::Failed(ExitClass::MergeRefused, r.detail),
        _ => MergeStep::Moved(r.main_head),
    })
}

/// Sweep dead attempts; every terminal path except `merged` runs this first.
async fn run_janitor(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    input: &CardInput,
    base_sha: &str,
) -> WorkflowResult<()> {
    let _removed: u32 = ctx
        .execute_activity(
            CardActivities::janitor,
            JanitorInput {
                repo: input.claim.repo.clone(),
                workroot: input.workroot.clone(),
                check_cmd: input.claim.check_cmd.clone(),
                base_sha: base_sha.to_string(),
            },
            janitor_options(cfg),
        )
        .await?;
    Ok(())
}

/// The options the `janitor` activity gets: the auxiliary options plus a
/// *detached* cancellation token.
///
/// `None` would inherit workflow cancellation, so a native cancel would abort
/// the sweep and leak the attempt's worktree. A detached token
/// (`WorkflowCancellationToken::new`) is not a child of the workflow token, so
/// workflow cancellation never reaches the janitor and the cleanup is not
/// skipped.
fn janitor_options(cfg: &Config) -> ActivityOptions {
    let mut opts = activity_options(cfg);
    opts.cancellation_token = Some(WorkflowCancellationToken::new());
    opts
}

/// Record a park packet and raise the alarm that goes with it.
async fn park_and_alarm(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    packet: &ParkPacket,
    reason: String,
) -> WorkflowResult<()> {
    ctx.execute_activity(
        CardActivities::park,
        packet.to_json(),
        activity_options(cfg),
    )
    .await?;
    ctx.execute_activity(
        CardActivities::alarm,
        Alarm {
            card: packet.card.clone(),
            reason,
        },
        activity_options(cfg),
    )
    .await?;
    Ok(())
}

/// The budget's view of the card's legs, marking the ones currently waited on.
fn legs_for(input: &CardInput, limited: &[String]) -> Vec<Leg> {
    input
        .legs
        .iter()
        .map(|l| Leg {
            id: l.id.clone(),
            tier: l.tier,
            harness: l.harness.clone(),
            limited: limited.iter().any(|id| id == &l.id),
        })
        .collect()
}

/// Length of the trailing run of `probe_fail` attempts, ignoring earlier ones.
fn probe_fail_streak(history: &[Attempt]) -> usize {
    history
        .iter()
        .rev()
        .take_while(|a| a.exit_class == ExitClass::ProbeFail)
        .count()
}

/// Time from `now` until `until`, saturating to zero once it has passed.
fn wait_duration(until: SystemTime, now: SystemTime) -> Duration {
    until.duration_since(now).unwrap_or(Duration::ZERO)
}

/// A park packet for a named attempt outcome.
fn packet_for(card: &str, tier: Tier, exit_class: ExitClass, check_tail: &str) -> ParkPacket {
    ParkPacket {
        card: card.to_string(),
        tier,
        exit_class,
        check_tail: check_tail.to_string(),
    }
}

/// A park packet for the newest attempt in `history`.
fn packet_from_last(card: &str, history: &[Attempt], check_tail: &str) -> ParkPacket {
    let last = history
        .last()
        .expect("the policy only acts on a non-empty history");
    packet_for(card, last.tier, last.exit_class, check_tail)
}

/// Stable context-class name, matching the JSON wire form of `ContextClass`
/// and the `class` field the projector reads.
fn context_class_name(c: ContextClass) -> &'static str {
    match c {
        ContextClass::K128 => "128k",
        ContextClass::K256 => "256k",
        ContextClass::M1 => "1m",
    }
}

/// Stable snake_case name, matching the JSON wire form of `ExitClass`.
fn exit_name(class: ExitClass) -> &'static str {
    match class {
        ExitClass::Ok => "ok",
        ExitClass::CheckFail => "check_fail",
        ExitClass::FlakyCheck => "flaky_check",
        ExitClass::UsageLimit => "usage_limit",
        ExitClass::Outage => "outage",
        ExitClass::LeaseLost => "lease_lost",
        ExitClass::WorkerCrash => "worker_crash",
        ExitClass::Stall => "stall",
        ExitClass::Timeout => "timeout",
        ExitClass::RebaseConflict => "rebase_conflict",
        ExitClass::MergeRefused => "merge_refused",
        ExitClass::ProbeFail => "probe_fail",
        ExitClass::ContractDefect => "contract_defect",
        ExitClass::NeedsInput => "needs_input",
        ExitClass::Cancelled => "cancelled",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn leg(id: &str, tier: Tier) -> LegSpec {
        LegSpec {
            id: id.to_string(),
            tier,
            model: "some/model".to_string(),
            harness: "opencode".to_string(),
        }
    }

    fn attempt(exit_class: ExitClass) -> Attempt {
        Attempt {
            exit_class,
            leg: "leg-1".to_string(),
            tier: Tier::Free,
            diff_hash: String::new(),
            check_hash: String::new(),
            context_overflow: false,
        }
    }

    /// A card input whose workroot is a real temporary directory.
    fn card_input(legs: Vec<LegSpec>) -> (TempDir, CardInput) {
        let root = tempfile::tempdir().unwrap();
        let claim = Claim {
            card: "c-1".to_string(),
            repo: "org/repo".to_string(),
            base_sha: "abc123".to_string(),
            check_cmd: "true".to_string(),
            allow: vec!["src/**".to_string()],
            forbid: Vec::new(),
            prompt: "do the thing".to_string(),
        };
        let input = CardInput {
            claim,
            legs,
            workroot: root.path().to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
            question_wait_cap_s: None,
        };
        (root, input)
    }

    #[test]
    fn legs_for_marks_limited_legs() {
        let (_root, input) = card_input(vec![leg("a", Tier::Free), leg("b", Tier::Paid)]);
        let legs = legs_for(&input, &["b".to_string()]);
        assert_eq!(
            legs,
            vec![
                Leg {
                    id: "a".to_string(),
                    tier: Tier::Free,
                    harness: "opencode".to_string(),
                    limited: false,
                },
                Leg {
                    id: "b".to_string(),
                    tier: Tier::Paid,
                    harness: "opencode".to_string(),
                    limited: true,
                },
            ]
        );
    }

    #[test]
    fn probe_fail_streak_counts_only_the_trailing_run() {
        assert_eq!(probe_fail_streak(&[]), 0);
        let history = vec![
            attempt(ExitClass::ProbeFail),
            attempt(ExitClass::CheckFail),
            attempt(ExitClass::ProbeFail),
            attempt(ExitClass::ProbeFail),
        ];
        assert_eq!(probe_fail_streak(&history), 2);
    }

    #[test]
    fn wait_duration_saturates_when_the_deadline_has_passed() {
        let now = UNIX_EPOCH + Duration::from_secs(100);
        assert_eq!(
            wait_duration(now + Duration::from_secs(5), now),
            Duration::from_secs(5)
        );
        assert_eq!(
            wait_duration(now - Duration::from_secs(5), now),
            Duration::ZERO
        );
        assert_eq!(wait_duration(now, now), Duration::ZERO);
    }

    #[test]
    fn auxiliary_activities_get_bounded_retries() {
        let cfg = Config::default();
        let retry = activity_options(&cfg)
            .retry_policy
            .expect("auxiliary options carry a retry policy");
        assert_eq!(retry.maximum_attempts(), 5);
        assert_eq!(retry.initial_interval(), Duration::from_secs(2));
    }

    #[test]
    fn merge_activity_is_single_attempt() {
        let cfg = Config::default();
        let retry = once_options(&cfg)
            .retry_policy
            .expect("merge options carry a retry policy");
        assert_eq!(retry.maximum_attempts(), 1);
    }

    #[test]
    fn janitor_is_detached_from_workflow_cancellation() {
        let cfg = Config::default();
        let token = janitor_options(&cfg)
            .cancellation_token
            .expect("the janitor names its own cancellation token");
        // A detached token never observes the workflow's cancellation, so the
        // sweep still runs to completion after a native cancel.
        assert!(!token.is_cancelled());
    }

    #[test]
    fn finish_maps_every_detail_to_a_projector_outcome() {
        // docs/projector.md contract v1: the workflow result is done | parked | failed.
        let cases = [
            ("merged", "done"),
            ("parked", "parked"),
            ("planner", "parked"),
            ("cancelled", "parked"),
            ("quarantined", "failed"),
            ("something_else", "failed"),
        ];
        for (detail, outcome) in cases {
            let out = finish(detail, None, 3);
            assert_eq!(out.detail, detail, "detail is carried verbatim");
            assert_eq!(out.outcome, outcome, "detail {detail}");
            assert_eq!(out.merged_sha, None);
            assert_eq!(out.attempts, 3);
        }
    }

    #[test]
    fn finish_keeps_the_merged_sha() {
        let out = finish("merged", Some("sha".to_string()), 7);
        assert_eq!(out.outcome, "done");
        assert_eq!(out.detail, "merged");
        assert_eq!(out.merged_sha.as_deref(), Some("sha"));
        assert_eq!(out.attempts, 7);
    }

    #[test]
    fn context_class_names_match_the_projector_wire_form() {
        assert_eq!(context_class_name(ContextClass::K128), "128k");
        assert_eq!(context_class_name(ContextClass::K256), "256k");
        assert_eq!(context_class_name(ContextClass::M1), "1m");
    }
}
