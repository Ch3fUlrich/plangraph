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
    CardActivities, CardInput, CardOutput, ConfineSpec, DoneInput, GateCheck, GateInput,
    GateResult, JanitorInput, MergeInput, MergeResult, ProbeInput, ProbeResult, Tick,
    park_json_with_attempt,
};
use crate::attempt::{AttemptInput, AttemptResult, LegSpec, to_budget_attempt};
use crate::keeper::{Alarm, AttemptReport, Claim, WORKER_LIVE_STATES};
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

/// Temporal patch marker for the card's gate running before a merge.
///
/// Scheduling the `gate` activity adds a command to the workflow history that a
/// history recorded before this change has no room for. `gate_step` asks the SDK
/// for this marker first, so an old in-flight card replays without the gate while
/// a card started after the change runs it. The id is pinned by a test: renaming
/// it would strand every card whose history already carries the old marker.
const GATE_PATCH: &str = "gate-before-merge-v1";

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

/// The `answer` signal, as the inbox sends it
/// (`src/plangraph/inbox/temporal_signal.py`): `{question_id, key, text, source}`.
/// Unknown fields are ignored and `key` and `source` may be missing; the id and
/// the text are required.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerSignal {
    #[serde(deserialize_with = "id_from_int_or_string")]
    pub question_id: i64,
    #[serde(default)]
    pub key: String,
    pub text: String,
    #[serde(default)]
    pub source: String,
}

/// A question id sent as a JSON integer or as a decimal string.
fn id_from_int_or_string<'de, D: serde::Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
    use serde::de::Error;
    match serde_json::Value::deserialize(d)? {
        serde_json::Value::Number(n) => n
            .as_i64()
            .ok_or_else(|| D::Error::custom("question_id is not an integer")),
        serde_json::Value::String(s) => s
            .trim()
            .parse()
            .map_err(|_| D::Error::custom("question_id is not a number")),
        _ => Err(D::Error::custom("question_id must be a number")),
    }
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
    /// the class goes to the policy, the text is the check tail, the last value
    /// is main's head at that moment (the base the retry must start from).
    Failed(ExitClass, String, String),
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

    /// Store a raw `answer` payload. One that does not parse is dropped with a
    /// log line: a bad signal must never fail the workflow task and wedge the
    /// card. An answer for another question is stored but never matches the
    /// wait, so it is not consumed.
    fn accept_answer(&mut self, raw: serde_json::Value) {
        match serde_json::from_value::<AnswerSignal>(raw) {
            Ok(mut a) => {
                // Cap before it enters workflow state and history, not only
                // where it is injected into a prompt.
                a.text = question::cap_text(&a.text);
                if self.answers.len() >= MAX_STORED_ANSWERS {
                    // Keep the newest: the oldest answer is long consumed.
                    eprintln!("answer signal buffer full: dropping the oldest answer");
                    self.answers.remove(0);
                }
                self.answers.push(a);
            }
            Err(e) => eprintln!("answer signal ignored: {e}"),
        }
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
        // Questions answered so far; at the limit the next one parks the card.
        let mut question_rounds: u32 = 0;
        let max_question_rounds = input.max_question_rounds.unwrap_or(cfg.max_question_rounds);

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
                        // Question parks take the live set: waiting is live (a
                        // timer or an exhausted attempt may have written it),
                        // claimed is not.
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
                                DoneInput {
                                    // Projector: merge retry timers can leave waiting; probe paths
                                    // can retain the attempt's needs_input/waiting state. No fixed CAS.
                                    expected_state: None,
                                    card: input.claim.card.clone(),
                                    merged_sha: sha.clone(),
                                    attempt: Some(n.max(1)),
                                },
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
                                // Park only live worker states; waiting is live
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
                    // Park only live worker states; waiting is live and claimed
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
                                // Park only live worker states; waiting is live
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
                                // Park only live worker states; waiting is live
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
                        // Park only live worker states; waiting is live and
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
                        // Park only live worker states; waiting is live and
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
                        // Park only live worker states; waiting is live and
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
    let cap = input
        .question_wait_cap_s
        .map_or(cfg.wait_cap, Duration::from_secs);
    // Clamped before it is sent: the keeper must not hold a default whose
    // deadline lies beyond the cap this workflow will park at.
    let q = q.normalized().clamped_to(cap);
    let plan = question::plan_wait(&q, cap);

    // An activity error here (after its transient retries) parks the card with
    // an alarm: the question could not be stored, so nobody can answer it.
    let asked: Result<Option<i64>, _> = ctx
        .execute_activity(
            CardActivities::ask_question,
            AskInput {
                // Questions carry the live set: a question asked after a timer
                // may find the card in waiting, and claimed must not pass.
                expected_state: Some(WORKER_LIVE_STATES.iter().map(|s| (*s).to_owned()).collect()),
                card: card.clone(),
                question: q.clone(),
                attempt: Some(n.max(1)),
            },
            activity_options(cfg),
        )
        .await;
    let asked = match asked {
        Ok(a) => a,
        Err(e) => {
            eprintln!("ask_question failed: {e}");
            return park_question(
                ctx,
                cfg,
                input,
                base_sha,
                tier,
                QUESTION_ASK_FAILED_REASON,
                q.text.clone(),
                n,
                // Failed asks park only on the live set; waiting is live,
                // claimed is not.
                WORKER_LIVE_STATES,
            )
            .await;
        }
    };
    let Some(qid) = asked else {
        return park_question(
            ctx,
            cfg,
            input,
            base_sha,
            tier,
            QUESTION_REFUSED_REASON,
            q.text.clone(),
            n,
            // Refused asks park only on the live set; waiting is live, claimed
            // is not.
            WORKER_LIVE_STATES,
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
                // Best effort: an activity error or timeout (after its retries)
                // counts as "not recorded". No `?` on this path: the window is
                // over and the card continues with the default regardless.
                let recorded: bool = match ctx
                    .execute_activity(
                        CardActivities::record_question_timeout,
                        qid,
                        activity_options(cfg),
                    )
                    .await
                {
                    Ok(r) => r,
                    Err(e) => {
                        eprintln!("record_question_timeout activity failed: {e}");
                        false
                    }
                };
                if !recorded {
                    // The default is still taken: the question window is over.
                    raise_alarm(
                        ctx,
                        cfg,
                        Alarm {
                            card: card.clone(),
                            reason: QUESTION_TIMEOUT_REFUSED_REASON.to_string(),
                            attempt: Some(n.max(1)),
                        },
                    )
                    .await;
                }
                Ok(QuestionStep::Answer(text))
            }
            OnTimer::Park => {
                park_question(
                    ctx,
                    cfg,
                    input,
                    base_sha,
                    tier,
                    QUESTION_PARK_REASON,
                    question::timeout_park_tail(&q.text),
                    n,
                    // Question timeouts park right after the deadline timer,
                    // which may have left the card waiting: the live set
                    // accepts waiting and still excludes claimed.
                    WORKER_LIVE_STATES,
                )
                .await
            }
        },
    }
}

/// End a card whose question cannot be answered: sweep, park and alarm.
/// The reason travels to the keeper in `check_tail` (its `/v1/park` only
/// accepts card/tier/exit_class/check_tail) and in the alarm.
#[allow(clippy::too_many_arguments)]
async fn park_question(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    input: &CardInput,
    base_sha: &str,
    tier: Tier,
    reason: &str,
    check_tail: String,
    n: u32,
    expected_state: &[&str],
) -> WorkflowResult<QuestionStep> {
    let packet = packet_for(&input.claim.card, tier, ExitClass::NeedsInput, &check_tail);
    run_janitor(ctx, cfg, input, base_sha).await?;
    // Question parks preserve newer actor states by accepting only the live
    // set, waiting included; the run/attempt fence and the keeper's idempotent
    // duplicate-park success keep a stale retry from double-parking.
    park_and_alarm_in_state(
        ctx,
        cfg,
        &packet,
        reason.to_string(),
        n.max(1),
        Some(expected_state),
    )
    .await?;
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
                worktree: format!(
                    "{}/{}",
                    input.workroot,
                    AttemptId::new(input.claim.card.clone(), n).dir_name()
                ),
                check_cmd: Some(input.claim.check_cmd.clone()),
                confine: Some(ConfineSpec {
                    allow: input.claim.allow.clone(),
                    forbid: input.claim.forbid.clone(),
                }),
            },
            once_options(cfg),
        )
        .await?;
    Ok(map_merge_result(r))
}

/// Map a merge activity result to the step the run loop acts on. Only the
/// outcomes the merge activity documents are understood; any other string is
/// refused (the park path), never guessed into a moved base or a conflict that
/// would loop or land work.
fn map_merge_result(r: MergeResult) -> MergeStep {
    match r.outcome.as_str() {
        "merged" => MergeStep::Merged(r.sha.unwrap_or(r.main_head)),
        "retry_later" => MergeStep::RetryLater,
        "base_moved" | "conflict" => MergeStep::Moved(r.main_head),
        "check_fail" => MergeStep::Failed(ExitClass::CheckFail, r.detail, r.main_head),
        "contract_defect" => MergeStep::Failed(ExitClass::ContractDefect, r.detail, r.main_head),
        "merge_refused" => MergeStep::Failed(ExitClass::MergeRefused, r.detail, r.main_head),
        other => MergeStep::Failed(
            ExitClass::MergeRefused,
            format!("unknown merge outcome {other:?}"),
            r.main_head,
        ),
    }
}

/// The card's named gate checks.
///
/// Today the list holds ONE check, `card-check`, whose command is the card's
/// own `check_cmd` — and is EMPTY when the card configures none. CARD-CHECKS
/// appends more named checks (e.g. a test-gaming diff scan) here: the wire
/// shape (`GateInput.checks`) is already a list, so a new check joins it
/// without another gate wiring change.
///
/// A blank command means the card has nothing to verify, so the list is empty
/// and the gate is skipped. The original string is kept, untrimmed: the shell
/// is what decides whitespace, and `merge_fenced` still runs an empty
/// `check_cmd` through `sh -c ''` (which passes). The `gate` activity instead
/// rejects an empty command as invalid input, so the workflow must never hand
/// it one — hence the empty list here and the trim test rather than an
/// emptiness test.
fn gate_checks(input: &CardInput) -> Vec<GateCheck> {
    let cmd = input.claim.check_cmd.as_str();
    if cmd.trim().is_empty() {
        return Vec::new();
    }
    vec![GateCheck {
        name: "card-check".to_string(),
        cmd: cmd.to_string(),
        timeout_s: None,
    }]
}

/// Run the card's gate in the attempt worktree before the branch is merged.
///
/// `Ok(None)` means the merge may proceed: there is no gate to run (the check
/// list is empty — the activity is never handed one, because an empty list is
/// invalid input there), or it passed. `Ok(Some(step))` means the gate blocked
/// the merge, and the step is exactly the one a merge-time check failure
/// produces (`CheckFail`, the gate's tail, main's head as the base), so the
/// run loop's `Failed` arm retries or parks the card with no gate-specific
/// case.
///
/// Replay safety comes first: scheduling the gate is a new command in the
/// history, so `ctx.patched` records (or replays) the marker before anything
/// else. `false` means the history predates the gate, so nothing is scheduled
/// and the old workflow replays unchanged; `true` means the gate runs. The check
/// is unconditional and outside any gate-specific branch, so every pass — a
/// first pass or one reached after a retry — takes the same path.
async fn gate_step(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    input: &CardInput,
    base_sha: &str,
    n: u32,
) -> WorkflowResult<Option<MergeStep>> {
    if !ctx.patched(GATE_PATCH) {
        return Ok(None);
    }
    let checks = gate_checks(input);
    if checks.is_empty() {
        return Ok(None);
    }
    let r: GateResult = match ctx
        .execute_activity(
            CardActivities::gate,
            GateInput {
                workroot: input.workroot.clone(),
                // The same worktree `merge_step` hands the merge: the gate must
                // judge the tree that will land, not a fresh checkout of it.
                workdir: format!(
                    "{}/{}",
                    input.workroot,
                    AttemptId::new(input.claim.card.clone(), n).dir_name()
                ),
                checks,
            },
            gate_options(cfg),
        )
        .await
    {
        Ok(r) => r,
        // A cancelled call ends the workflow the same way every other activity
        // call in this file does.
        Err(e) if e.as_cancelled().is_some() => return Err(e.into()),
        // A gate that could not run must never fall through to the merge, so it
        // parks the card; the merge refusal class is what the policy grows into.
        Err(e) => {
            return Ok(Some(MergeStep::Failed(
                ExitClass::MergeRefused,
                format!("gate activity failed: {e}"),
                base_sha.to_string(),
            )));
        }
    };
    Ok(map_gate_result(r, base_sha))
}

/// Map a gate result to the step the run loop acts on: `None` when the gate
/// passed and the merge may go ahead.
///
/// A gate that does not pass — any check failed, timed out, or was never run
/// for lack of budget — is a merge-time check failure. The tail is built from
/// the FAILING checks only, each named, so a report says which check blocked
/// the merge; passing checks are omitted to keep the tail on the failure. The
/// join is capped to `redact::MAX_TAIL_BYTES` (keep the end, cut on a char
/// boundary). `base_sha` is passed through unchanged: a blocked gate never
/// moves main.
fn map_gate_result(r: GateResult, base_sha: &str) -> Option<MergeStep> {
    if r.passed {
        return None;
    }
    let failing: Vec<String> = r
        .checks
        .iter()
        .filter(|c| !c.passed)
        .map(|c| {
            // A timed-out check says so, in its own block, before its tail.
            let body = if c.timed_out {
                format!("timed out\n{}", c.tail)
            } else {
                c.tail.clone()
            };
            format!("[{}]\n{}", c.name, body)
        })
        .collect();
    let tail = redact::cap_tail_bytes(&failing.join("\n\n"), redact::MAX_TAIL_BYTES);
    Some(MergeStep::Failed(
        ExitClass::CheckFail,
        tail,
        base_sha.to_string(),
    ))
}

/// The options the `gate` activity gets: it heartbeats, so it carries a
/// heartbeat timeout, and a gate may be re-run a bounded number of times before
/// the run loop treats the failure as it does any other activity error.
fn gate_options(cfg: &Config) -> ActivityOptions {
    let retry = RetryPolicy::builder()
        .initial_interval(Duration::from_secs(2))
        .maximum_attempts(3)
        .build();
    ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
        .heartbeat_timeout(cfg.heartbeat_timeout)
        .retry_policy(retry)
        .build()
}

/// True once the keeper merge lock has stayed unavailable for more than
/// [`MERGE_RETRY_CAP`] retries: the card parks instead of waiting again.
fn merge_retry_exhausted(retries: u32) -> bool {
    retries > MERGE_RETRY_CAP
}

/// Carry main's head forward as the base for the next worktree. An empty head
/// (an activity that could not read it) keeps the old base.
fn adopt_base(base_sha: &mut String, main_head: String) {
    if !main_head.is_empty() {
        *base_sha = main_head;
    }
}

/// Record the merge-time verdict on attempt `n`: it is that attempt's own
/// (last) entry, so it is replaced, never followed by a second entry that would
/// make the budget count one attempt twice.
fn replace_attempt(history: &mut Vec<Attempt>, judged: Attempt) {
    match history.last_mut() {
        Some(last) => *last = judged,
        None => history.push(judged),
    }
}

/// Tell the keeper the merge-time verdict on attempt `n`.
///
/// The attempt was reported with its own exit class before it was merged;
/// `replace_attempt` then corrected only the workflow history, so the keeper
/// would still hold `ok` for an attempt that failed its post-rebase check. This
/// sends the same attempt number again with the final class so both agree.
/// Best effort: the correction must not take the card down, and a keeper that
/// keeps attempt rows append-only may refuse the repeat.
#[allow(clippy::too_many_arguments)]
async fn rereport_judged(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    input: &CardInput,
    n: u32,
    leg_id: &str,
    exit_class: ExitClass,
    diff_hash: &str,
    check_tail: &str,
) {
    let report = judged_report(
        &input.claim.card,
        n,
        leg_id,
        exit_class,
        diff_hash,
        check_tail,
    );
    let _ = ctx
        .execute_activity(CardActivities::report, report, activity_options(cfg))
        .await;
}

/// The corrected report for attempt `n`: same number, final exit class.
fn judged_report(
    card: &str,
    n: u32,
    leg_id: &str,
    exit_class: ExitClass,
    diff_hash: &str,
    check_tail: &str,
) -> AttemptReport {
    AttemptReport {
        card: card.to_string(),
        n,
        leg: leg_id.to_string(),
        exit_class,
        diff_hash: diff_hash.to_string(),
        check_tail: check_tail.to_string(),
    }
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
                card: input.claim.card.clone(),
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
///
/// `attempt` is the card attempt number (`n`, 1-based) the park happened on:
/// it rides in the packet JSON and on the alarm so the keeper can fence both
/// calls to this run of this attempt, never Temporal's activity retry count.
async fn park_and_alarm(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    packet: &ParkPacket,
    reason: String,
    attempt: u32,
) -> WorkflowResult<()> {
    // Generic parks CAS on the one live set (waiting included: the projector
    // writes it on usage_limit/outage attempts and TIMER_STARTED). Excluding a
    // stale retry is the run/attempt fence's and the keeper's idempotent
    // duplicate-park success's job, never this list's; claimed stays out.
    park_and_alarm_in_state(ctx, cfg, packet, reason, attempt, Some(WORKER_LIVE_STATES)).await
}

async fn park_and_alarm_in_state(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    packet: &ParkPacket,
    reason: String,
    attempt: u32,
    expected_state: Option<&[&str]>,
) -> WorkflowResult<()> {
    ctx.execute_activity(
        CardActivities::park,
        park_json_with_attempt(
            packet,
            Some(attempt),
            expected_state.map(|states| states.iter().map(|state| (*state).to_owned()).collect()),
        ),
        activity_options(cfg),
    )
    .await?;
    raise_alarm(
        ctx,
        cfg,
        Alarm {
            card: packet.card.clone(),
            reason,
            attempt: Some(attempt),
        },
    )
    .await;
    Ok(())
}

/// Raise `alarm`, best effort: a short bounded retry, and a failure is dropped.
/// The alarm is a notification; losing it must not fail or hang a card whose
/// state is already recorded (parked, or continuing with a default).
async fn raise_alarm(ctx: &WorkflowContext<Card>, cfg: &Config, alarm: Alarm) {
    let retry = RetryPolicy::builder()
        .initial_interval(Duration::from_secs(1))
        .maximum_attempts(3)
        .build();
    let opts = ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
        .retry_policy(retry)
        .build();
    let _ = ctx
        .execute_activity(CardActivities::alarm, alarm, opts)
        .await;
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
    use crate::activities::GateCheckResult;
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
            max_question_rounds: None,
        };
        (root, input)
    }

    #[test]
    fn answer_signal_tolerates_extra_and_missing_optional_fields() {
        let a: AnswerSignal = serde_json::from_value(serde_json::json!({
            "question_id": 7, "text": "yes", "extra": {"a": 1}, "more": null,
        }))
        .unwrap();
        assert_eq!(
            a,
            AnswerSignal {
                question_id: 7,
                key: String::new(),
                text: "yes".into(),
                source: String::new(),
            }
        );
        let full: AnswerSignal = serde_json::from_value(serde_json::json!({
            "question_id": "12", "key": "k", "text": "t", "source": "answer",
        }))
        .unwrap();
        assert_eq!((full.question_id, full.key.as_str()), (12, "k"));
    }

    #[test]
    fn an_answer_for_another_question_is_not_consumed() {
        let mut card = Card::default();
        card.accept_answer(serde_json::json!({"question_id": 2, "text": "for two"}));
        assert_eq!(card.answer_text(1), None);
        card.accept_answer(serde_json::json!({"question_id": 1, "text": "for one"}));
        assert_eq!(card.answer_text(1).as_deref(), Some("for one"));
        // The other one is still there for its own question.
        assert_eq!(card.answer_text(2).as_deref(), Some("for two"));
    }

    #[test]
    fn unparsable_answers_are_ignored_not_fatal() {
        let mut card = Card::default();
        for bad in [
            serde_json::json!("text"),
            serde_json::json!(null),
            serde_json::json!({"text": "no id"}),
            serde_json::json!({"question_id": 1}),
            serde_json::json!({"question_id": 1.5, "text": "t"}),
            serde_json::json!({"question_id": [1], "text": "t"}),
            serde_json::json!({"question_id": 1, "text": 9}),
        ] {
            card.accept_answer(bad);
        }
        assert!(card.answers.is_empty());
        // A question_timeout signal is stored but is never an answer.
        card.accept_answer(
            serde_json::json!({"question_id": 3, "text": "d", "source": "question_timeout"}),
        );
        assert_eq!(card.answer_text(3), None);
    }

    #[test]
    fn stored_answers_are_bounded() {
        let mut card = Card::default();
        for i in 0..(MAX_STORED_ANSWERS as i64 + 10) {
            card.accept_answer(serde_json::json!({"question_id": i, "text": "t"}));
        }
        assert_eq!(card.answers.len(), MAX_STORED_ANSWERS);
        // The newest are kept, the oldest dropped.
        assert_eq!(
            card.answers.last().unwrap().question_id,
            MAX_STORED_ANSWERS as i64 + 9
        );
        assert_eq!(card.answers[0].question_id, 10);
    }

    #[test]
    fn stored_answer_text_is_capped() {
        let mut card = Card::default();
        let long = "x".repeat(question::MAX_TEXT_CHARS + 500);
        card.accept_answer(serde_json::json!({"question_id": 1, "text": long}));
        let got = card.answer_text(1).unwrap();
        assert!(got.ends_with(question::TRUNCATION_MARKER));
        assert_eq!(
            got.chars().count(),
            question::MAX_TEXT_CHARS + question::TRUNCATION_MARKER.chars().count()
        );
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

    fn merge_result_of(outcome: &str) -> MergeResult {
        MergeResult {
            outcome: outcome.to_string(),
            sha: Some("s".to_string()),
            main_head: "h".to_string(),
            detail: "d".to_string(),
        }
    }

    #[test]
    fn unknown_merge_outcome_is_refused_not_moved() {
        for o in ["", "moved", "MERGED", "rebase_conflict", "something_new"] {
            match map_merge_result(merge_result_of(o)) {
                MergeStep::Failed(ExitClass::MergeRefused, d, h) => {
                    assert_eq!(h, "h");
                    assert!(d.contains("unknown merge outcome"), "{o}: {d}")
                }
                other => panic!("{o:?} mapped to {other:?}"),
            }
        }
        assert!(matches!(
            map_merge_result(merge_result_of("base_moved")),
            MergeStep::Moved(h) if h == "h"
        ));
        assert!(matches!(
            map_merge_result(merge_result_of("conflict")),
            MergeStep::Moved(_)
        ));
        assert!(matches!(
            map_merge_result(merge_result_of("merged")),
            MergeStep::Merged(s) if s == "s"
        ));
    }

    #[test]
    fn merge_time_failures_carry_main_head_as_the_new_base() {
        for o in ["check_fail", "contract_defect", "merge_refused"] {
            match map_merge_result(merge_result_of(o)) {
                MergeStep::Failed(_, _, h) => assert_eq!(h, "h", "{o}"),
                other => panic!("{o}: {other:?}"),
            }
        }
        let mut base = "old".to_string();
        adopt_base(&mut base, "new".to_string());
        assert_eq!(base, "new");
        adopt_base(&mut base, String::new());
        assert_eq!(base, "new", "an empty head keeps the base");
    }

    fn gate_check_result(name: &str, passed: bool, timed_out: bool, tail: &str) -> GateCheckResult {
        GateCheckResult {
            name: name.to_string(),
            passed,
            exit_code: None,
            signal: None,
            timed_out,
            tail: tail.to_string(),
            output_hash: String::new(),
        }
    }

    fn gate_result(passed: bool, checks: Vec<GateCheckResult>) -> GateResult {
        GateResult { passed, checks }
    }

    #[test]
    fn gate_checks_is_empty_for_a_blank_check_cmd() {
        for blank in ["", " ", "\t\n  "] {
            let (_root, mut input) = card_input(vec![leg("leg-1", Tier::Free)]);
            input.claim.check_cmd = blank.to_string();
            // A card with no check command has nothing to verify: the check
            // list is empty and `gate_step` skips the gate entirely — the
            // activity is never handed an empty list, which it rejects as
            // invalid input.
            assert!(gate_checks(&input).is_empty(), "{blank:?}");
        }
    }

    #[test]
    fn gate_checks_names_one_card_check_with_the_untrimmed_cmd() {
        let (_root, mut input) = card_input(vec![leg("leg-1", Tier::Free)]);
        input.claim.check_cmd = "  cargo test  ".to_string();
        let checks = gate_checks(&input);
        assert_eq!(checks.len(), 1);
        assert_eq!(checks[0].name, "card-check");
        // The original string is kept, not a trimmed copy: the shell owns
        // whitespace.
        assert_eq!(checks[0].cmd, "  cargo test  ");
        // Unset: the activity shares its `start_to_close` budget across checks.
        assert_eq!(checks[0].timeout_s, None);
    }

    #[test]
    fn map_gate_result_is_none_only_when_the_gate_passed() {
        assert!(map_gate_result(gate_result(true, vec![]), "base").is_none());
        // `passed` is the authority: a stale nonzero exit code does not block.
        assert!(
            map_gate_result(
                gate_result(
                    true,
                    vec![gate_check_result("card-check", true, false, "boom")]
                ),
                "base"
            )
            .is_none()
        );
    }

    #[test]
    fn map_gate_result_blocks_a_failure_with_check_fail() {
        let step = map_gate_result(
            gate_result(
                false,
                vec![gate_check_result("card-check", false, false, "boom")],
            ),
            "base",
        );
        match step {
            Some(MergeStep::Failed(ExitClass::CheckFail, tail, base)) => {
                assert_eq!(tail, "[card-check]\nboom");
                // A blocked gate leaves main alone, so the base is main's head.
                assert_eq!(base, "base");
            }
            other => panic!("a failed gate must block the merge, got {other:?}"),
        }
    }

    #[test]
    fn map_gate_result_marks_a_timeout_in_the_tail() {
        let step = map_gate_result(
            gate_result(
                false,
                vec![gate_check_result(
                    "card-check",
                    false,
                    true,
                    "partial output",
                )],
            ),
            "base",
        );
        let Some(MergeStep::Failed(ExitClass::CheckFail, tail, base)) = step else {
            panic!("a timed-out gate must block the merge");
        };
        assert!(tail.contains("[card-check]"), "{tail}");
        assert!(tail.contains("timed out\npartial output"), "{tail}");
        assert_eq!(base, "base");
    }

    #[test]
    fn map_gate_result_names_each_failing_check_and_omits_passing_ones() {
        let r = gate_result(
            false,
            vec![
                gate_check_result("unit", false, false, "unit broke"),
                gate_check_result("diff-scan", true, false, "clean"),
                gate_check_result("lint", false, true, "lint output"),
            ],
        );
        match map_gate_result(r, "base") {
            Some(MergeStep::Failed(ExitClass::CheckFail, tail, _)) => {
                assert!(tail.contains("[unit]\nunit broke"), "{tail}");
                assert!(tail.contains("[lint]\ntimed out\nlint output"), "{tail}");
                // The failing blocks are joined by a blank line...
                assert!(tail.contains("unit broke\n\n[lint]"), "{tail}");
                // ...and the passing check is not named anywhere.
                assert!(
                    !tail.contains("diff-scan") && !tail.contains("clean"),
                    "{tail}"
                );
            }
            other => panic!("a failed gate must block the merge, got {other:?}"),
        }
    }

    #[test]
    fn map_gate_result_tail_is_capped_keeping_the_end() {
        let big = "x".repeat(redact::MAX_TAIL_BYTES + 1000);
        let step = map_gate_result(
            gate_result(
                false,
                vec![gate_check_result("card-check", false, false, &big)],
            ),
            "base",
        );
        let Some(MergeStep::Failed(ExitClass::CheckFail, tail, _)) = step else {
            panic!("a failed gate must block the merge");
        };
        assert!(tail.len() <= redact::MAX_TAIL_BYTES, "{}", tail.len());
        // The cap keeps the END: what is kept is a suffix of the check's tail.
        assert!(big.ends_with(&tail), "tail is not a suffix of the source");
    }

    #[test]
    fn gate_options_are_bounded() {
        let cfg = Config::default();
        let opts = gate_options(&cfg);
        assert_eq!(
            opts.close_timeouts.start_to_close(),
            Some(cfg.start_to_close)
        );
        assert_eq!(opts.heartbeat_timeout, Some(cfg.heartbeat_timeout));
        let retry = opts
            .retry_policy
            .expect("the gate options carry a retry policy");
        assert_eq!(retry.maximum_attempts(), 3);
        assert_eq!(retry.initial_interval(), Duration::from_secs(2));
    }

    #[test]
    fn gate_patch_id_is_stable() {
        // Renaming the marker would strand every card whose history already
        // carries it, so the id is pinned.
        assert_eq!(GATE_PATCH, "gate-before-merge-v1");
    }

    #[test]
    fn a_merge_time_verdict_replaces_the_attempts_entry_not_adds_one() {
        // Attempt 1 ran ok (entry pushed), then failed its post-rebase check.
        let mut history = vec![attempt(ExitClass::Ok)];
        replace_attempt(&mut history, attempt(ExitClass::CheckFail));
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].exit_class, ExitClass::CheckFail);
        // Only the judged entry consumes budget: one attempt, not two.
        assert_eq!(history.iter().filter(|a| a.consumes_attempt()).count(), 1);
        // An earlier attempt's entry is untouched.
        let mut two = vec![attempt(ExitClass::CheckFail), attempt(ExitClass::Ok)];
        replace_attempt(&mut two, attempt(ExitClass::RebaseConflict));
        assert_eq!(two.len(), 2);
        assert_eq!(two[0].exit_class, ExitClass::CheckFail);
        assert_eq!(two[1].exit_class, ExitClass::RebaseConflict);
    }

    #[test]
    fn a_merge_time_verdict_is_re_reported_with_the_same_attempt_number() {
        // The keeper first saw attempt 3 as `ok`; the merge-time check failed.
        let r = judged_report("c-1", 3, "leg-a", ExitClass::CheckFail, "dh", "tail");
        assert_eq!((r.card.as_str(), r.n, r.leg.as_str()), ("c-1", 3, "leg-a"));
        assert_eq!(r.exit_class, ExitClass::CheckFail);
        assert_eq!(
            (r.diff_hash.as_str(), r.check_tail.as_str()),
            ("dh", "tail")
        );
    }

    #[test]
    fn retry_later_cap_parks_with_merge_refused() {
        assert!(!merge_retry_exhausted(0));
        assert!(!merge_retry_exhausted(MERGE_RETRY_CAP));
        assert!(merge_retry_exhausted(MERGE_RETRY_CAP + 1));
        // The park packet the loop builds for it.
        let p = packet_for(
            "c-1",
            Tier::Free,
            ExitClass::MergeRefused,
            MERGE_LOCK_PARK_REASON,
        );
        assert_eq!(p.exit_class, ExitClass::MergeRefused);
        assert_eq!(p.check_tail, MERGE_LOCK_PARK_REASON);
        assert!(matches!(
            map_merge_result(merge_result_of("retry_later")),
            MergeStep::RetryLater
        ));
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
