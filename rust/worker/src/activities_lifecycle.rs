
/// The process-local fallback when heartbeats or cancellation cannot be spelled
/// into an `ActivityError` directly: stringify and wrap.
/// How a keeper failure should be treated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Failure {
    /// The keeper said no; asking again cannot change that: 401, 403, 409, 422, a park `Conflict`.
    Refused,
    /// Plausibly gone in a moment: a transport error, 408, 429 or any 5xx.
    Transient,
    /// Anything else (another 4xx, an undecodable reply): not retried here.
    Other,
}
fn classify(e: &crate::keeper::KeeperError) -> Failure {
    use crate::keeper::KeeperError;
    match e {
        KeeperError::Http(_) => Failure::Transient,
        KeeperError::Conflict(_) | KeeperError::Status(401 | 403 | 409 | 422) => Failure::Refused,
        KeeperError::Status(408 | 429 | 500..=599) => Failure::Transient,
        KeeperError::Status(_) | KeeperError::Decode(_) => Failure::Other,
    }
}
fn finish_is_refusal(e: &crate::keeper::KeeperError) -> bool {
    classify(e) == Failure::Refused || matches!(e, crate::keeper::KeeperError::Status(400))
}
/// Pauses between the tries of a keeper call that failed transiently, so a call
/// is made at most `1 + len` times.
const TRANSIENT_BACKOFF: [Duration; 3] = [
    Duration::from_millis(250),
    Duration::from_secs(1),
    Duration::from_secs(3),
];
/// Run `f`; while it fails transiently sleep through `backoff` and run it
/// again. Blocking: call from `spawn_blocking`.
fn retry_transient<T>(
    backoff: &[Duration],
    mut f: impl FnMut() -> Result<T, crate::keeper::KeeperError>,
) -> Result<T, crate::keeper::KeeperError> {
    let mut pauses = backoff.iter();
    loop {
        match f() {
            Err(e) if classify(&e) == Failure::Transient => match pauses.next() {
                Some(d) => std::thread::sleep(*d),
                None => return Err(e),
            },
            other => return other,
        }
    }
}
/// A permanent refusal (see [`Failure::Refused`]) is logged and reported as
/// `None`; any other failure is an error the activity retry policy handles.
fn settle_refusal<T>(
    res: Result<T, crate::keeper::KeeperError>,
    verb: &str,
) -> Result<Option<T>, ActivityError> {
    match res {
        Ok(v) => Ok(Some(v)),
        Err(e) if classify(&e) == Failure::Refused => {
            eprintln!("keeper refused {verb}: {e}");
            Ok(None)
        }
        Err(e) => Err(activity_error(e)),
    }
}
fn activity_error(e: impl std::fmt::Display) -> ActivityError {
    anyhow::anyhow!(e.to_string()).into()
}
/// Invalid gate input (empty command, timeout 0, a workdir outside the workroot
/// or not a directory) can never succeed on a retry, so it is reported as a
/// non-retryable application failure; any other io error keeps the default
/// retry behaviour.
fn gate_error(e: std::io::Error) -> ActivityError {
    if e.kind() == std::io::ErrorKind::InvalidInput {
        ActivityError::application(temporalio_sdk::ApplicationFailure::non_retryable(
            e.to_string(),
        ))
    } else {
        activity_error(e)
    }
}
// Old activity histories carry a single string; keep them readable on replay.
fn deserialize_expected_state<'de, D>(deserializer: D) -> Result<Option<Vec<String>>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum States {
        One(String),
        Any(Vec<String>),
    }
    Ok(
        Option::<States>::deserialize(deserializer)?.map(|states| match states {
            States::One(state) => vec![state],
            States::Any(states) => states,
        }),
    )
}
/// The execution identity for a keeper call made by this activity: workflow
/// and run from the activity context, the attempt from the activity input —
/// the *card* attempt number, monotone per run, never Temporal's
/// `ActivityInfo::attempt` (the activity retry count, which resets per
/// activity and would be refused as lower by the Python keeper).
fn exec_of(
    ctx: &ActivityContext,
    attempt: Option<u32>,
    expected_state: Option<Vec<String>>,
) -> ExecId {
    let info = ctx.info();
    ExecId {
        workflow_id: info.workflow_id.clone(),
        run_id: info.workflow_run_id.clone(),
        attempt,
        expected_state,
    }
}
/// The card attempt a `park` activity input carries as an extra `"attempt"`
/// key beside the packet fields, `None` when absent, unparseable or outside
/// `u32` (an unknown key is ignored by `ParkPacket::from_json`, so a bad value
/// only costs the identity, never the park itself).
fn park_attempt_of(input: &str) -> Option<u32> {
    serde_json::from_str::<serde_json::Value>(input)
        .ok()?
        .get("attempt")?
        .as_u64()?
        .try_into()
        .ok()
}
/// The `expected_state` a `park` activity input carries: the observation the
/// workflow captured when it decided to park, or its live-set fallback when it
/// observed nothing. `None` when the key is absent or empty (the park then
/// sends no state CAS, as it did before the key existed).
///
/// This is the whole of the park path's state handling (G3-1): the activity
/// reads the key and forwards it, and never calls `Keeper::card_state`.
pub(crate) fn expected_state_of(input: &str) -> Option<Vec<String>> {
    let state = serde_json::from_str::<serde_json::Value>(input)
        .ok()?
        .get("expected_state")?
        .clone();
    if let Some(state) = state.as_str() {
        return Some(vec![state.to_owned()]);
    }
    serde_json::from_value::<Vec<String>>(state)
        .ok()
        .filter(|states| !states.is_empty())
}
/// The `observe` read the park decision gets: `Keeper::card_state` (a blocking
/// ureq call) off the tokio worker threads in `spawn_blocking`, waiting at most
/// [`OBSERVE_READ_TIMEOUT`] for it (G3-4). `Ok(None)` -- a keeper that holds
/// no state -- means "observed nothing", so the workflow parks on its live-set
/// fallback. `Err` (D1b) is a transient read failure -- a timeout, a panicking
/// blocking task, D1a's keeper `Err` -- which the activity turns into a
/// retryable failure so Temporal re-runs the read inside one scheduled
/// activity before anyone degrades.
pub(crate) async fn observe_read(
    keeper: Arc<dyn Keeper>,
    card: String,
) -> Result<Option<String>, ActivityError> {
    observe_read_within(keeper, card, OBSERVE_READ_TIMEOUT).await
}
/// [`observe_read`] with an explicit bound, so the timeout is testable.
async fn observe_read_within(
    keeper: Arc<dyn Keeper>,
    card: String,
    limit: Duration,
) -> Result<Option<String>, ActivityError> {
    let read = tokio::task::spawn_blocking(move || keeper.card_state(&card));
    match tokio::time::timeout(limit, read).await {
        Ok(read) => read
            .map_err(join_error)
            .and_then(|res| res.map_err(activity_error)),
        // Timed out: a transient failure to retry. The blocking task is
        // detached (it can only finish or sit on its own socket timeout).
        Err(_) => Err(activity_error("observe read timed out")),
    }
}
fn join_error(e: tokio::task::JoinError) -> ActivityError {
    if e.is_panic() {
        let p = e.into_panic();
        let msg = if let Some(s) = p.downcast_ref::<&str>() {
            (*s).to_string()
        } else if let Some(s) = p.downcast_ref::<String>() {
            s.clone()
        } else {
            "unknown panic cause".to_string()
        };
        activity_error(format!("park blocking task panicked: {msg}"))
    } else {
        activity_error(e)
    }
}
/// Park activity failure typing, crate-public so the keeper's 409-body tests can assert it (R4).
pub(crate) fn keeper_park_error(e: KeeperError) -> ActivityError {
    if classify(&e) == Failure::Refused {
        // D2/R4: only the `exec_stale_state` code -- a park-path `Conflict` --
        // is typed; any other 409 stays non-retryable but untyped.
        if matches!(&e, KeeperError::Conflict(c) if c == crate::keeper::STALE_STATE_FAILURE) {
            return ActivityError::application(
                temporalio_sdk::ApplicationFailure::builder(anyhow::anyhow!(e.to_string()))
                    .type_name(crate::keeper::STALE_STATE_FAILURE.to_owned())
                    .non_retryable(true)
                    .build(),
            );
        }
        ActivityError::application(temporalio_sdk::ApplicationFailure::non_retryable(
            e.to_string(),
        ))
    } else {
        activity_error(e)
    }
}
/// The whole of what the `park` activity runs below its context binding: parse
/// the packet from the input and hand the keeper the exec identity the
/// activity built -- workflow/run from the context, the card attempt and the
/// recorded observation (`expected_state`) from the input, verbatim.
///
/// Extracted so a test can run exactly this path (V7-5): an `ActivityContext`
/// cannot be built outside the SDK, but everything observation-shaped -- the
/// parse, the forwarding, the keeper CAS -- lives here, and nothing in it ever
/// calls `Keeper::card_state` (G3-1): a retried park presents the observation
/// the workflow recorded when it decided, never a fresher read.
pub(crate) async fn park_path(
    keeper: Arc<dyn Keeper>,
    input: String,
    exec: ExecId,
) -> Result<(), ActivityError> {
    let packet = ParkPacket::from_json(&input).map_err(|e| activity_error(e.to_string()))?;
    tokio::task::spawn_blocking(move || keeper.park(&packet, &exec).map_err(keeper_park_error))
        .await
        .map_err(join_error)?
}
/// The park packet JSON the workflow hands to the `park` activity, with the
/// card attempt and the `expected_state` the workflow recorded added as extra
/// top-level keys when known. [`ParkPacket::from_json`] ignores the extra keys
/// (the activity reads them back there to build the [`ExecId`] and forwards the
/// `expected_state` verbatim -- the observation the workflow captured when it
/// decided to park, or its live-set fallback), so the packet itself round-trips.
pub fn park_json_with_attempt(
    p: &ParkPacket,
    attempt: Option<u32>,
    expected_state: Option<Vec<String>>,
) -> String {
    let mut v: serde_json::Value =
        serde_json::from_str(&p.to_json()).expect("packet serializes to JSON");
    if let Some(obj) = v.as_object_mut() {
        if let Some(attempt) = attempt {
            obj.insert("attempt".into(), serde_json::json!(attempt));
        }
        if let Some(state) = expected_state {
            obj.insert(
                "expected_state".into(),
                if state.len() == 1 {
                    serde_json::json!(state[0])
                } else {
                    serde_json::json!(state)
                },
            );
        }
    }
    v.to_string()
}
/// A wall-clock tick with fresh jitter. Nondeterminism stays in the activity so
/// the workflow replaying the same call is not affected.
pub(crate) fn tick_now() -> Tick {
    let unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    Tick {
        unix_ms,
        jitter: budget::random_jitter(),
    }
}
/// Name a probe outcome, mapping any failure to `error` because a probe that
/// could not run is not evidence the attempt did nothing.
pub(crate) fn map_probe(r: Result<ProbeOutcome, ProbeError>, branch: String) -> ProbeResult {
    let outcome = match r {
        Ok(ProbeOutcome::AlreadyDone) => "already_done",
        Ok(ProbeOutcome::Partial) => "partial",
        Ok(ProbeOutcome::Nothing) => "nothing",
        Err(_) => "error",
    };
    ProbeResult {
        outcome: outcome.to_string(),
        branch,
    }
}
/// Name a merge outcome. `main_head` is the branch after the attempt, so a
/// `base_moved` caller sees what it must rebase onto.
pub(crate) fn map_merge(o: MergeOutcome, main_head: String) -> MergeResult {
    match o {
        MergeOutcome::Merged { sha } => MergeResult {
            outcome: "merged".to_string(),
            sha: Some(sha),
            main_head,
            detail: String::new(),
        },
        MergeOutcome::BaseMoved => MergeResult {
            outcome: "base_moved".to_string(),
            sha: None,
            main_head,
            detail: String::new(),
        },
        MergeOutcome::Conflict => MergeResult {
            outcome: "conflict".to_string(),
            sha: None,
            main_head,
            detail: String::new(),
        },
    }
}
/// Probe the worktree of `card#n` in `workroot`. A missing worktree or a bad
/// base is reported as `error`, never as `nothing`.
pub(crate) fn do_probe(cfg: &Config, input: &ProbeInput) -> ProbeResult {
    let id = AttemptId::new(input.card.clone(), input.n);
    let dir = Path::new(&input.workroot).join(id.dir_name());
    let outcome = probe::probe(
        &dir,
        &input.base_sha,
        &input.check_cmd,
        cfg.start_to_close,
        cfg.check_tail_lines,
    );
    map_probe(outcome, id.branch())
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PhaseFinishInput {
    pub key: String,
    pub branch: String,
    pub head: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pre_patch_phase: Option<String>,
}
pub(crate) fn do_phase_finish(
    keeper: &dyn Keeper,
    key: &str,
    branch: &str,
    head: &str,
    pre_patch_phase: Option<&str>,
) -> Result<crate::phase::PhaseFinish, crate::keeper::KeeperError> {
    retry_transient(&TRANSIENT_BACKOFF, || {
        keeper.phase_finish(key, branch, head, pre_patch_phase)
    })
}
pub(crate) fn do_phase_plan(
    keeper: &dyn Keeper,
    key: &str,
) -> Result<crate::phase::PhasePlan, crate::keeper::KeeperError> {
    retry_transient(&TRANSIENT_BACKOFF, || keeper.phase_plan(key))
}
pub(crate) fn do_claim_card(
    keeper: &dyn Keeper,
    key: &str,
) -> Result<Option<Claim>, crate::keeper::KeeperError> {
    retry_transient(&TRANSIENT_BACKOFF, || {
        keeper.claim_card(merge_owner_id(), key)
    })
}
pub(crate) fn admitted_claim(
    keeper: &dyn Keeper,
    cfg: &Config,
    key: &str,
) -> Result<Option<Claim>, ActivityError> {
    crate::card_admit::allow(cfg, key)?;
    crate::card_admit::claimed(
        settle_refusal(do_claim_card(keeper, key), "claim_card").map(Option::flatten)?,
        key,
    )
}
pub(crate) fn do_ensure_phase_branch(
    input: &PhaseBranchInput,
) -> Result<PhaseBranchResult, phase_branch_io::BranchError> {
    ensure_phase_branch_with(input, git)
}
