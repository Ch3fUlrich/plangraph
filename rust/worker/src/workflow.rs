//! The `card` workflow: one workflow per card, one pass per attempt.
//!
//! Everything here must be deterministic on replay, so the clock and jitter
//! arrive only through the `tick` activity, the retry policy is the pure
//! `pg_worker_core::budget::next_action`, and the history is a plain `Vec`.
//! A pass runs one attempt on a fresh worktree, records it, then either merges
//! an `ok` result or asks the budget for the next move: retry, re-route, wait,
//! promote, upgrade the context class, quarantine or park.
use crate::activities::{
    AskInput, CardActivities, CardOutput, ConfineSpec, DoneInput, GateCheck, GateInput, GateResult,
    JanitorInput, MergeInput, MergeResult, ProbeInput, ProbeResult, Tick, park_json_with_attempt,
};
use crate::attempt::{AttemptInput, AttemptResult, LegSpec, to_budget_attempt};
use crate::keeper::{
    Alarm, AttemptReport, Claim, STALE_STATE_FAILURE, WORKER_LIVE_STATES, expected_state_for,
};
use crate::question::{self, OnTimer, Question};
use pg_worker_core::budget::{self, Action, Attempt, ContextClass, Leg};
use pg_worker_core::config::{Config, Tier};
use pg_worker_core::exit_class::ExitClass;
use pg_worker_core::packet::ParkPacket;
use pg_worker_core::redact;
use pg_worker_core::worktree::AttemptId;
use serde::{Deserialize, Serialize};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use temporalio_common::RetryPolicy;
use temporalio_common::protos::temporal::api::failure::v1::{Failure, failure::FailureInfo};
use temporalio_macros::{workflow, workflow_methods};
use temporalio_sdk::{
    ActivityOptions, ApplicationFailure, SyncWorkflowContext, TimeoutType,
    WorkflowCancellationToken, WorkflowContext, WorkflowResult, WorkflowTermination,
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
/// Temporal patch marker for the card's gate running before a merge.
///
/// Scheduling the `gate` activity adds a command to the workflow history that a
/// history recorded before this change has no room for. `gate_step` asks the SDK
/// for this marker first, so an old in-flight card replays without the gate while
/// a card started after the change runs it. The id is pinned by a test: renaming
/// it would strand every card whose history already carries the old marker.
const GATE_PATCH: &str = "gate-before-merge-v1";
/// `ctx.patched` marker for the `observe` activity that captures the state a
/// park presents (EXEC-OBSERVED-STATE, G3-1). Scheduling it is a new command,
/// so a history recorded before the marker replays without it and parks on the
/// live-set fallback, exactly as those runs did.
const OBSERVE_PATCH: &str = "park-observe-v1";
/// `ctx.patched` marker for the park 409 (`exec_stale_state`) re-observe loop
/// (D4). The loop issues `observe`/`park` commands a history recorded before
/// the marker has no room for, so an old in-flight card keeps parking once and
/// returning the refusal, exactly as it did. The id is pinned by a test:
/// renaming it would strand every card whose history already carries the old
/// marker.
const PARK_REOBSERVE_PATCH: &str = "park-reobserve-v1";
/// How many times a park refused as stale is re-observed and retried before the
/// workflow fails with the last cause (D3).
const PARK_REOBSERVES: u32 = 2;
pub use crate::card_input::CardInput;
/// Alarm reason when a question stayed unanswered until the wait cap.
const QUESTION_PARK_REASON: &str = question::TIMEOUT_PARK_REASON;

/// Alarm reason when the keeper would not store the agent's question.
const QUESTION_REFUSED_REASON: &str = "question not recorded";

/// Alarm reason when the `ask_question` activity failed even after its retries.
const QUESTION_ASK_FAILED_REASON: &str = "question not recorded (keeper unreachable)";

/// Alarm reason when the default was taken but the keeper refused to record it.
const QUESTION_TIMEOUT_REFUSED_REASON: &str = "question_timeout not recorded";

/// Alarm reason when a card asked more questions than `max_question_rounds`.
const QUESTION_ROUNDS_REASON: &str = "question rounds exhausted";

/// Answer signals kept per card; a flood beyond this is dropped.
const MAX_STORED_ANSWERS: usize = 256;
include!("workflow_state.rs");
#[workflow_methods]
impl Card {
    #[run]
    pub async fn run(
        ctx: &mut WorkflowContext<Self>,
        mut input: CardInput,
    ) -> WorkflowResult<CardOutput> {
        crate::card_memo::upsert(ctx, &input.claim.card)?;
        // Constant policy/options, rebuilt like all local state on replay.
        let cfg = Config::default();
        // A child of the workflow token, so native cancel and the signal both reach
        // the activity.
        let op = ctx.cancellation_token().child_token();
        ctx.state_mut(|s| s.op = Some(op.clone()));
        // A signal can be applied before this body runs, when `op` was still empty.
        if ctx.state(|s| s.cancel_requested) {
            op.cancel_with_reason("cancel before start".to_string());
        }

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
        // Questions answered so far; at the limit the next one parks the card.
        let mut question_rounds: u32 = 0;
        let max_question_rounds = input.max_question_rounds.unwrap_or(cfg.max_question_rounds);
        let bounded = ctx.patched(crate::rollover::LIMITS_PATCH);
        if let Some(r) = input.resume.take() {
            history = r.history;
            base_sha = r.base_sha;
            n = r.n;
            waits = r.waits;
            waited = r.waited;
            context = r.context;
            limited = r.limited;
            leg = r.leg;
            answers = r.answers;
            question_rounds = r.question_rounds;
            ctx.state_mut(|s| s.answers.splice(0..0, r.signals).for_each(drop));
        }
        let initial_waits = waits;
        loop {
            if bounded
                && n > 0
                && crate::rollover::due(
                    ctx.history_length(),
                    waits - initial_waits,
                    ctx.continue_as_new_suggested(),
                )
                && !ctx.state(|s| s.cancel_requested)
            {
                input.resume = Some(crate::rollover::Resume {
                    history,
                    base_sha,
                    n,
                    waits,
                    waited,
                    context,
                    limited,
                    leg,
                    answers,
                    question_rounds,
                    signals: ctx.state(|s| s.answers.clone()),
                });
                return ctx
                    .continue_as_new(input, Default::default())
                    .map(|v| match v {});
            }
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
            let mut opts = ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
                .heartbeat_timeout(cfg.heartbeat_timeout)
                .retry_policy(retry)
                .cancellation_token(op.clone())
                .build();
            if bounded {
                crate::rollover::bound(&mut opts, cfg.start_to_close);
            }
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
            let attempt_input = crate::card_input::projector_input(ctx, attempt_input);
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
                        branch: AttemptId::new(input.claim.card.clone(), n).branch(),
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

            // An already-green change check is a broken contract, not work a
            // retry or an already-done probe can repair.
            if result.exit_class == ExitClass::ContractDefect
                && check_tail.starts_with("red-first:")
            {
                let packet = packet_for(
                    &input.claim.card,
                    leg.tier,
                    ExitClass::ContractDefect,
                    &check_tail,
                );
                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                park_and_alarm(
                    ctx,
                    &cfg,
                    &packet,
                    "red-first contract defect".into(),
                    n.max(1),
                )
                .await?;
                return Ok(parked(n));
            }

            // A question is not a failure: it takes no retry budget, so the
            // attempt stays out of the history. The next attempt reruns the
            // same leg with the answer in its prompt.
            if result.exit_class == ExitClass::NeedsInput
                && let Some(q) = &result.question
            {
                if question_rounds >= max_question_rounds {
                    // Park without asking: the keeper would only hold one more
                    // question nobody is meant to answer.
                    return match park_question(
                        ctx,
                        &cfg,
                        &input,
                        &base_sha,
                        leg.tier,
                        QUESTION_ROUNDS_REASON,
                        q.normalized().text.clone(),
                        n,
                        // Question parks hand over the live set as the
                        // fallback (EXEC-OBSERVED-STATE): the activity sends
                        // the state it observed instead, and waiting is live
                        // there because a timer or an exhausted attempt may
                        // have written it; claimed is not in the set.
                        WORKER_LIVE_STATES,
                    )
                    .await?
                    {
                        QuestionStep::Finished(out) => Ok(out),
                        QuestionStep::Answer(_) => unreachable!("park_question always finishes"),
                    };
                }
                match question_step(ctx, &cfg, &input, &base_sha, leg.tier, q, n).await? {
                    QuestionStep::Answer(text) => {
                        question_rounds += 1;
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
                // Step 4: merge an `ok` (or probed already-done) attempt now, but
                // only after the card's gate has passed on the exact tree about to
                // be merged. A gate that blocks produces the same step a merge-time
                // check failure would, so the arms below need no gate case.
                if let Some(branch) = branch_to_merge.take() {
                    let step = match gate_step(ctx, &cfg, &input, &base_sha, n).await? {
                        Some(blocked) => blocked,
                        None => merge_step(ctx, &cfg, &input, &base_sha, &branch, n).await?,
                    };
                    match step {
                        MergeStep::Merged(sha) => {
                            ctx.execute_activity(
                                CardActivities::complete,
                                // Merge retry timers can leave waiting; probe paths
                                // can retain the attempt's needs_input/waiting
                                // state -- the live set covers every one of them,
                                // so the real complete carries a CAS like the
                                // other worker verbs instead of `None` (sq5 fix 4).
                                done_input(input.claim.card.clone(), sha.clone(), n),
                                activity_options(&cfg),
                            )
                            .await?;
                            run_janitor(ctx, &cfg, &input, &base_sha).await?;
                            return Ok(finish("merged", Some(sha), n));
                        }
                        MergeStep::RetryLater => {
                            merge_retries += 1;
                            if merge_retry_exhausted(merge_retries) {
                                let packet = packet_for(
                                    &input.claim.card,
                                    leg.tier,
                                    ExitClass::MergeRefused,
                                    MERGE_LOCK_PARK_REASON,
                                );
                                run_janitor(ctx, &cfg, &input, &base_sha).await?;
                                // Park only live worker states as the fallback; waiting is live
                                // and claimed is not.
                                park_and_alarm(
                                    ctx,
                                    &cfg,
                                    &packet,
                                    MERGE_LOCK_PARK_REASON.to_string(),
                                    n.max(1),
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
                        MergeStep::Failed(class, tail, main_head) => {
                            // The branch was judged under the lock; probing it again
                            // would only find the same commits and loop back here.
                            probed = true;
                            check_tail = tail;
                            // The retry starts from where main is now, not from the
                            // stale base the failed merge was rebased away from.
                            adopt_base(&mut base_sha, main_head);
                            // This is the same attempt `n`, now judged: it replaces
                            // its entry instead of adding a second one (budget).
                            replace_attempt(
                                &mut history,
                                Attempt {
                                    exit_class: class,
                                    leg: leg.id.clone(),
                                    tier: leg.tier,
                                    diff_hash: diff_hash.clone(),
                                    check_hash: check_hash.clone(),
                                    context_overflow: false,
                                },
                            );
                            rereport_judged(
                                ctx,
                                &cfg,
                                &input,
                                n,
                                &leg.id,
                                class,
                                &diff_hash,
                                &check_tail,
                            )
                            .await;
                        }
                        MergeStep::Moved(main_head) => {
                            // The worker already tried to rebase this attempt onto the
                            // moved main and could not (or had no worktree to try in).
                            // Take the new head as the base and let the policy retry
                            // from a fresh worktree; probing the branch again would
                            // only re-merge the same commits and count the conflict twice.
                            probed = true;
                            adopt_base(&mut base_sha, main_head);
                            replace_attempt(
                                &mut history,
                                Attempt {
                                    exit_class: ExitClass::RebaseConflict,
                                    leg: leg.id.clone(),
                                    tier: leg.tier,
                                    diff_hash: diff_hash.clone(),
                                    check_hash: check_hash.clone(),
                                    context_overflow: false,
                                },
                            );
                            rereport_judged(
                                ctx,
                                &cfg,
                                &input,
                                n,
                                &leg.id,
                                ExitClass::RebaseConflict,
                                &diff_hash,
                                &check_tail,
                            )
                            .await;
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
                    // Park only live worker states as the fallback; waiting is live and claimed
                    // is not.
                    park_and_alarm(
                        ctx,
                        &cfg,
                        &packet,
                        PROBE_FAIL_PARK_REASON.to_string(),
                        n.max(1),
                    )
                    .await?;
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
                                // Park only live worker states as the fallback; waiting is live
                                // and claimed is not.
                                park_and_alarm(
                                    ctx,
                                    &cfg,
                                    &packet,
                                    exit_name(packet.exit_class).to_string(),
                                    n.max(1),
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
                                // Park only live worker states as the fallback; waiting is live
                                // and claimed is not.
                                park_and_alarm(
                                    ctx,
                                    &cfg,
                                    &packet,
                                    exit_name(packet.exit_class).to_string(),
                                    n.max(1),
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
                            // Wait-cap park: the projector may have left the
                            // card in waiting (a usage_limit/outage attempt or
                            // TIMER_STARTED) and the live set accepts it;
                            // claimed is still excluded.
                            park_and_alarm(
                                ctx,
                                &cfg,
                                &packet,
                                WAIT_CAP_PARK_REASON.to_string(),
                                n.max(1),
                            )
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
                        // Park only live worker states as the fallback; waiting is live and
                        // claimed is not.
                        park_and_alarm(
                            ctx,
                            &cfg,
                            &packet,
                            exit_name(packet.exit_class).to_string(),
                            n.max(1),
                        )
                        .await?;
                        return Ok(finish("quarantined", None, n));
                    }
                    Action::ToPlanner => {
                        let packet = packet_from_last(&input.claim.card, &history, &check_tail);
                        run_janitor(ctx, &cfg, &input, &base_sha).await?;
                        // Park only live worker states as the fallback; waiting is live and
                        // claimed is not.
                        park_and_alarm(
                            ctx,
                            &cfg,
                            &packet,
                            exit_name(packet.exit_class).to_string(),
                            n.max(1),
                        )
                        .await?;
                        return Ok(finish("planner", None, n));
                    }
                    Action::Park { packet } => {
                        run_janitor(ctx, &cfg, &input, &base_sha).await?;
                        // Park only live worker states as the fallback; waiting is live and
                        // claimed is not.
                        park_and_alarm(
                            ctx,
                            &cfg,
                            &packet,
                            exit_name(packet.exit_class).to_string(),
                            n.max(1),
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
    pub fn answer(&mut self, _ctx: &mut SyncWorkflowContext<Self>, a: serde_json::Value) {
        self.accept_answer(a);
    }

    #[signal]
    pub fn cancel(&mut self, _ctx: &mut SyncWorkflowContext<Self>, reason: String) {
        self.cancel_requested = true;
        if let Some(op) = &self.op {
            op.cancel_with_reason(reason);
        }
    }
}
include!("workflow_question.rs");
include!("workflow_gate.rs");
include!("workflow_park.rs");
#[cfg(test)]
mod tests {
    include!("workflow_regression_1.rs");
    include!("workflow_regression_2.rs");
}
