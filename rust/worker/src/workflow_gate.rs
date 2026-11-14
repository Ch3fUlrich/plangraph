
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
/// Builtins always run, even when no shell check was configured. A required
/// but missing heldout command fails closed instead of silently disappearing.
fn gate_checks(input: &CardInput) -> Vec<GateCheck> {
    let mut checks = Vec::new();
    let mut command = |name: String, cmd: String| {
        checks.push(GateCheck {
            name,
            cmd,
            builtin: None,
            timeout_s: None,
        })
    };
    if !input.claim.check_cmd.trim().is_empty() {
        command("check".into(), input.claim.check_cmd.clone());
    }
    if input.risk_tagged || !input.heldout_cmd.trim().is_empty() {
        command(
            "heldout".into(),
            if input.heldout_cmd.trim().is_empty() {
                "false".into()
            } else {
                input.heldout_cmd.clone()
            },
        );
    }
    for (i, cmd) in input.smoke_checks.iter().enumerate() {
        command(format!("smoke-{}", i + 1), cmd.clone());
    }
    for builtin in ["diffscan", "exports"] {
        checks.push(GateCheck {
            name: builtin.into(),
            cmd: builtin.into(),
            builtin: Some(builtin.into()),
            timeout_s: None,
        });
    }
    checks
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
            gate_input(input, base_sha, n, checks),
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

fn gate_input(input: &CardInput, base_sha: &str, n: u32, checks: Vec<GateCheck>) -> GateInput {
    GateInput {
        workroot: input.workroot.clone(),
        // Judge the tree that will land, against its current attempt base.
        workdir: format!(
            "{}/{}",
            input.workroot,
            AttemptId::new(input.claim.card.clone(), n).dir_name()
        ),
        checks,
        base_sha: Some(base_sha.to_string()),
        exports: input.exports.clone(),
    }
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
