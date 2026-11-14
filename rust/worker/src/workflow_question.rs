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

/// The input the real `complete` activity gets for a merged attempt.
///
/// RT-P1 sq5 fix 4 (deepseek MED, D-2410 HIGH): complete sends the live set the
/// worker observed instead of `None`, so the keeper's `_worker_complete` writes
/// with a real state CAS instead of "whatever the card happens to be now".  The
/// set is [`WORKER_LIVE_STATES`]: merge-retry timers can leave `waiting` and
/// probe paths can retain the attempt's `needs_input`/`waiting` state, and all
/// of those are live; `claimed` and the terminal states are not.
fn done_input(card: String, merged_sha: String, attempt: u32) -> DoneInput {
    DoneInput {
        expected_state: Some(WORKER_LIVE_STATES.iter().map(|s| (*s).to_owned()).collect()),
        card,
        merged_sha,
        attempt: Some(attempt.max(1)),
    }
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
                // Questions carry the live set as an any-of CAS: an ask never
                // moves the card, so it keeps the set the ADR documents (G3-3)
                // and is not narrowed to a single observed state. A question
                // asked after a timer may find the card in waiting, and claimed
                // must not pass.
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
                // Failed asks park on the live-set fallback; waiting is live,
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
            // Refused asks park on the live-set fallback; waiting is live,
            // claimed is not.
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
                    // which may have left the card waiting: the fallback
                    // accepts waiting and still excludes claimed, and the
                    // activity sends the observed waiting state instead when
                    // it read one.
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
    // Question parks preserve newer actor states by handing over only the
    // live-set fallback (waiting included, claimed not); the workflow's
    // `observe` activity captures the state at decision time and that is what
    // the activity sends (G3-1), with the run/attempt fence plus the keeper's
    // idempotent duplicate-park success keeping a stale retry from
    // double-parking.
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
