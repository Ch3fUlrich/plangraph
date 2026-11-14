
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

/// The observation the `park` that follows gets (EXEC-OBSERVED-STATE, G3-1):
/// `None` when the history predates the marker (an in-flight run replays the
/// way it always did) or when the observe retries ran out (D1b: the cause is
/// logged) -- the caller then parks on its live-set fallback. A cancelled read
/// ends the workflow the way every other activity call here does.
async fn observe_card(
    ctx: &WorkflowContext<Card>,
    cfg: &Config,
    card: &str,
) -> WorkflowResult<Option<String>> {
    if !ctx.patched(OBSERVE_PATCH) {
        return Ok(None);
    }
    match ctx
        .execute_activity(
            CardActivities::observe,
            card.to_string(),
            activity_options(cfg),
        )
        .await
    {
        Ok(observed) => Ok(observed),
        Err(e) if e.as_cancelled().is_some() => Err(e.into()),
        // D1b: the activity's own retries are exhausted. Degrade to the
        // live-set fallback as before -- but the cause stays visible.
        Err(e) => {
            eprintln!("observe retries exhausted, parking on fallback: {e}");
            Ok(None)
        }
    }
}

/// D2: the typed park refusal node in a failure's cause chain, if any. The
/// chain is walked, not just the top node: an activity failure wraps the
/// application failure the `park` activity returned as its cause.
fn stale_state_refusal(failure: Option<&Failure>) -> Option<&Failure> {
    let mut f = failure;
    while let Some(node) = f {
        if let Some(FailureInfo::ApplicationFailureInfo(info)) = &node.failure_info
            && info.r#type == STALE_STATE_FAILURE
        {
            return Some(node);
        }
        f = node.cause.as_deref();
    }
    None
}

/// D2: did the `park` activity fail with the typed state-CAS refusal (409,
/// `exec_stale_state`) -- the one failure a fresh observation can repair?
fn is_stale_state_refusal(failure: Option<&Failure>) -> bool {
    stale_state_refusal(failure).is_some()
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
    // Generic parks hand the activity the live set as the FALLBACK
    // (EXEC-OBSERVED-STATE): `park_and_alarm_in_state` swaps in the state the
    // workflow's `observe` activity captured when it decided to park.
    // `waiting` is in the fallback
    // because the projector writes it on usage_limit/outage attempts and on
    // TIMER_STARTED. Excluding a stale retry is the run/attempt fence's and
    // the keeper's idempotent duplicate-park success's job, never this list's;
    // claimed stays out.
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
    // EXEC-OBSERVED-STATE (G3-1): the observation is captured HERE, when the
    // workflow decides to park, and travels in the activity input. The `park`
    // activity never re-reads the keeper, so a Temporal retry of a delayed park
    // presents this same state and is refused when the card moved on, instead
    // of reading whatever the projector wrote meanwhile. Because the read moved
    // out of the park call, the park itself lands while `GET /v1/show` is down
    // (G3-2); when the observation could not be made, `expected_state_for`
    // keeps the live-set fallback the workflow hands over.
    //
    // D2/D4: a park the keeper refused 409 is the state-CAS miss
    // (exec_stale_state) -- the card moved on since the observation -- and the
    // one failure a fresh observation can repair, so re-observe and park once
    // more, at most PARK_REOBSERVES times (D3: the refused cause is logged on
    // every re-observe, and the failure that ends the loop carries the last
    // one). Every other error returns at once; without the marker a run parks
    // once and returns the refusal, exactly as before this loop existed.
    let mut reobserves = 0;
    loop {
        let observed = observe_card(ctx, cfg, &packet.card).await?;
        let states = expected_state_for(
            observed,
            expected_state.map(|states| states.iter().map(|s| (*s).to_owned()).collect()),
        );
        match ctx
            .execute_activity(
                CardActivities::park,
                park_json_with_attempt(packet, Some(attempt), states),
                activity_options(cfg),
            )
            .await
        {
            Ok(()) => break,
            Err(e) if e.as_cancelled().is_some() => return Err(e.into()),
            Err(e) if ctx.patched(PARK_REOBSERVE_PATCH) && is_stale_state_refusal(e.failure()) => {
                let cause = stale_state_refusal(e.failure())
                    .map(|f| f.message.as_str())
                    .unwrap_or("unknown cause");
                if reobserves == PARK_REOBSERVES {
                    // D3: the loop is spent: the last cause plus the count.
                    return Err(ApplicationFailure::builder(anyhow::anyhow!(
                        "park refused by keeper as stale after {reobserves} re-observes: {cause}"
                    ))
                    .non_retryable(true)
                    .build()
                    .into());
                }
                reobserves += 1;
                eprintln!(
                    "park refused as stale ({STALE_STATE_FAILURE}), re-observe {reobserves}/{PARK_REOBSERVES}: {cause}"
                );
            }
            Err(e) => return Err(e.into()),
        }
    }
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
        ExitClass::NoChange => "no_change",
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
