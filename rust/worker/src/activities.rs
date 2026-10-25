//! Temporal activities and the wire types they exchange with the card workflow.
//!
//! Every activity here is a thin, side-effecting shell: the deterministic parts
//! live in `pg_worker_core` (probe, janitor, budget) and the blocking parts in
//! the sibling modules (attempt, mergelock, keeper). The activity body owns the
//! engine-specific concerns the workflow must not have: wall clock, jitter,
//! heartbeats and cancellation. The mapping helpers are factored out as plain
//! `pub(crate)` functions so they can be tested without a Temporal server.

use crate::attempt::{self, AttemptInput, AttemptResult, LegSpec};
use crate::harness::HarnessRegistry;
use crate::keeper::{Alarm, AttemptReport, Claim, ExecId, Fence, Keeper};
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

/// Task queue the card workflow and its activities share.
pub const TASK_QUEUE: &str = "pg-cards";

/// How long a worker waits for a competing merge before giving up.
const MERGE_LOCK_TIMEOUT: Duration = Duration::from_secs(30);
/// Upper bound on one `observe` read (G3-4): the blocking keeper call runs in
/// `spawn_blocking`, and the activity stops waiting for it after 5 s so a hung
/// keeper cannot hold a park decision hostage.
const OBSERVE_READ_TIMEOUT: Duration = Duration::from_secs(5);

/// The merge activity's own budget: the start-to-close window the workflow gives
/// it (`workflow::once_options`), and the single value everything inside the
/// merge is derived from.
pub(crate) fn merge_window(cfg: &Config) -> Duration {
    cfg.start_to_close
}

/// Time kept back at the end of the merge window for what follows the
/// post-rebase check: the fast-forward, the release of the keeper lock and the
/// activity result. A quarter of the window, at most a minute.
pub(crate) fn merge_reserve(cfg: &Config) -> Duration {
    (merge_window(cfg) / 4).min(Duration::from_secs(60))
}

/// Budget for rebase, confinement recheck and post-rebase check together:
/// strictly less than the merge window, so the check is killed (and judged
/// `check_fail`) before Temporal times the activity out while a detached
/// blocking thread keeps running it.
pub(crate) fn merge_work_budget(cfg: &Config) -> Duration {
    merge_window(cfg).saturating_sub(merge_reserve(cfg))
}

/// Seconds the keeper-held merge lock stays valid without a release.
///
/// Derived from the merge window: the lock is taken before the file lock, so it
/// must outlive the whole merge activity, i.e. the window plus the wait for a
/// competing merge ([`MERGE_LOCK_TIMEOUT`]). A fixed 120 s could expire under a
/// slow check and let a second merger in while this one was still running.
fn merge_lock_ttl_s(cfg: &Config) -> u64 {
    merge_window(cfg).as_secs().max(1) + MERGE_LOCK_TIMEOUT.as_secs()
}

/// Input of the `card` workflow: the claim, the legs it may run on, and where.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CardInput {
    pub claim: Claim,
    pub legs: Vec<LegSpec>,
    pub workroot: String,
    pub main_branch: String,
    /// Longest the card waits for an answer to a question, in seconds. Unset =
    /// the configured wait cap (24 h). Exists so a test need not wait a day.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub question_wait_cap_s: Option<u64>,
    /// Questions answered before the next one parks the card. Unset = the
    /// configured `max_question_rounds` (3). Injectable so a test is quick.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_question_rounds: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhasePlanInput {
    pub key: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClaimCardInput {
    pub key: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhaseBranchInput {
    pub repo: String,
    pub base_branch: String,
    pub branch: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PhaseBranchResult {
    pub head: String,
    pub created: bool,
}

/// Ask the keeper to store an agent question for a card.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AskInput {
    /// The any-of `expected_state` the workflow hands over (the live set):
    /// an ask keeps the any-of CAS the ADR documents and never narrows it
    /// (G3-3), and the activity forwards it verbatim without re-reading
    /// the keeper (G3-1).
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "deserialize_expected_state"
    )]
    pub expected_state: Option<Vec<String>>,
    pub card: String,
    pub question: Question,
    /// The card attempt number (monotone per run), not Temporal's activity
    /// retry count. Unset on older inputs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u32>,
}

/// Result of the `card` workflow.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CardOutput {
    /// Projector-contract value: `done` | `parked` | `failed`. The projector
    /// reads only this field, so it must never carry a worker-specific word.
    pub outcome: String,
    /// The worker's specific word: `merged` | `parked` | `quarantined` |
    /// `planner` | `cancelled`. Kept separate so the projector, which reads only
    /// `outcome`, is not exposed to the finer-grained vocabulary.
    #[serde(default)]
    pub detail: String,
    pub merged_sha: Option<String>,
    pub attempts: u32,
}

/// A clock sample taken inside an activity, never in the workflow.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Tick {
    pub unix_ms: u64,
    pub jitter: f64,
}

/// Ask an attempt worktree what it already left behind.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProbeInput {
    pub repo: String,
    pub workroot: String,
    pub card: String,
    pub n: u32,
    pub base_sha: String,
    pub check_cmd: String,
}

/// Outcome of a probe: `already_done` | `partial` | `nothing` | `error`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProbeResult {
    pub outcome: String,
    pub branch: String,
}

/// One named check of a card's gate: a shell command and an optional per-check
/// timeout. A list of named checks — never a single command — is the wire
/// shape, so more checks (a test-gaming diff scan, say) can be appended
/// without another gate wiring change.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateCheck {
    /// Matched by `[A-Za-z0-9_.-]{1,64}`; duplicates are invalid input, since
    /// a report could not tell two same-named runs apart.
    pub name: String,
    /// The command, run via `sh -c`. Builtins ignore it, but still require a
    /// non-blank placeholder to preserve the wire validation contract.
    pub cmd: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub builtin: Option<String>,
    /// Kill this check after this many seconds; unset = whatever is left of
    /// the gate's `start_to_close` budget when the check starts.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_s: Option<u64>,
}

/// Input of the `gate` activity: the named checks to run, in list order, in
/// one confined directory. The top-level `timeout_s` of the single-command
/// gate is gone: time is one budget the whole list shares.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateInput {
    /// The card work root the workdir must stay inside.
    pub workroot: String,
    /// Directory the checks run in; confined to `workroot`: relative or
    /// absolute, no `..`, symlinks resolved, never outside the root.
    pub workdir: String,
    /// The checks, run sequentially in list order. Required on the wire (no
    /// serde default): a missing field must fail the deserialisation exactly
    /// as the single `cmd` before it did, and an empty list is invalid input,
    /// never a silent pass.
    pub checks: Vec<GateCheck>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub base_sha: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub exports: Vec<String>,
}

/// Outcome of ONE gate check: the `check::CheckResult` fields plus the
/// check's name, so a failing tail is reported under the name that failed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateCheckResult {
    pub name: String,
    pub passed: bool,
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub timed_out: bool,
    /// The check's own redacted, line- and byte-capped tail — or the reason
    /// it did not run.
    pub tail: String,
    /// Hash of `tail`, for "identical failure" comparisons.
    pub output_hash: String,
}

/// Outcome of a gate run: `passed` only when EVERY check passed. A failing
/// check does not stop the later ones, so a later diff scan still reports.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateResult {
    pub passed: bool,
    pub checks: Vec<GateCheckResult>,
}

/// The write set a merge re-checks under the lock.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConfineSpec {
    pub allow: Vec<String>,
    pub forbid: Vec<String>,
}

/// Fast-forward an attempt branch into main, guarded by the file lock and the
/// local CAS (the effective exclusion on a single worker host per repo); the
/// keeper lock and fencing token wrap it but only coordinate hosts once a real
/// keeper implements the lock endpoint (see [`crate::mergelock`]'s header).
/// Every field after `base_sha` is optional so a bare ff merge (and older
/// recorded inputs) still deserialises.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MergeInput {
    pub repo: String,
    pub main_branch: String,
    pub branch: String,
    pub base_sha: String,
    /// The attempt worktree (branch checked out). Needed to rebase when main
    /// moved; empty means a moved base is only reported.
    #[serde(default)]
    pub worktree: String,
    /// Re-run once after a rebase; `None` skips it.
    #[serde(default)]
    pub check_cmd: Option<String>,
    /// Write-set recheck under the lock; `None` skips it.
    #[serde(default)]
    pub confine: Option<ConfineSpec>,
}

/// Outcome of a merge: `merged` | `base_moved` | `conflict` | `retry_later`
/// (keeper lock unavailable) | `merge_refused` (stale fencing token) |
/// `contract_defect` (confinement violated under the lock) | `check_fail`
/// (the post-rebase check failed). `detail` carries a check tail or reason.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MergeResult {
    pub outcome: String,
    pub sha: Option<String>,
    pub main_head: String,
    #[serde(default)]
    pub detail: String,
}

/// Tell the keeper a card is done.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DoneInput {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "deserialize_expected_state"
    )]
    pub expected_state: Option<Vec<String>>,
    pub card: String,
    pub merged_sha: String,
    /// The card attempt number (monotone per run), not Temporal's activity
    /// retry count. Unset on older inputs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u32>,
}

/// Sweep dead attempts of one repo.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JanitorInput {
    pub repo: String,
    pub workroot: String,
    pub check_cmd: String,
    pub base_sha: String,
    /// Card whose attempts are swept; empty sweeps every attempt of the repo (legacy input).
    #[serde(default)]
    pub card: String,
}

/// Everything the activities need, injected so tests can stub the seams.
#[derive(Clone)]
pub struct Deps {
    pub keeper: Arc<dyn Keeper>,
    pub harnesses: HarnessRegistry,
    pub cfg: Config,
    pub patterns: Patterns,
}

/// Temporal activities over the engine-free worker modules.
pub struct CardActivities {
    deps: Deps,
}

impl CardActivities {
    pub fn new(deps: Deps) -> Self {
        CardActivities { deps }
    }
}

/// The process-local fallback when heartbeats or cancellation cannot be spelled
/// into an `ActivityError` directly: stringify and wrap.
/// How a keeper failure should be treated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Failure {
    /// The keeper said no and asking again cannot change that: 401, 403, 409, 422.
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
        KeeperError::Status(401 | 403 | 409 | 422) => Failure::Refused,
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
/// [`OBSERVE_READ_TIMEOUT`] for it (G3-4). `None` -- timeout, a panicking
/// blocking task, a keeper that answered nothing -- means "observed nothing",
/// so the workflow parks on its live-set fallback.
pub(crate) async fn observe_read(keeper: Arc<dyn Keeper>, card: String) -> Option<String> {
    observe_read_within(keeper, card, OBSERVE_READ_TIMEOUT).await
}

/// [`observe_read`] with an explicit bound, so the timeout is testable.
async fn observe_read_within(
    keeper: Arc<dyn Keeper>,
    card: String,
    limit: Duration,
) -> Option<String> {
    let read = tokio::task::spawn_blocking(move || keeper.card_state(&card));
    match tokio::time::timeout(limit, read).await {
        Ok(read) => read.ok().flatten(),
        // Timed out: observed nothing. The blocking task is detached (it can
        // only finish or sit on its own socket timeout); the activity returns.
        Err(_) => None,
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
    tokio::task::spawn_blocking(move || keeper.park(&packet, &exec).map_err(activity_error))
        .await
        .map_err(activity_error)?
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
}

pub(crate) fn do_phase_finish(
    keeper: &dyn Keeper,
    key: &str,
    branch: &str,
    head: &str,
) -> Result<crate::phase::PhaseFinish, crate::keeper::KeeperError> {
    retry_transient(&TRANSIENT_BACKOFF, || {
        keeper.phase_finish(key, branch, head)
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

pub(crate) fn do_ensure_phase_branch(
    input: &PhaseBranchInput,
) -> Result<PhaseBranchResult, String> {
    if input.repo.is_empty() || input.base_branch.is_empty() || input.branch.is_empty() {
        return Err("repo, base_branch and branch are required".to_string());
    }
    check_rev(&input.base_branch).map_err(|e| e.to_string())?;
    check_rev(&input.branch).map_err(|e| e.to_string())?;

    if !input.branch.starts_with("phase/") {
        return Err("not a phase/ branch".to_string());
    }
    if input.branch == input.base_branch {
        return Err("phase branch must not be the base branch".to_string());
    }

    let repo = Path::new(&input.repo);
    let base_ref = format!("refs/heads/{}", input.base_branch);
    let branch_ref = format!("refs/heads/{}", input.branch);
    let base_head = git(repo, &["rev-parse", base_ref.as_str()]).map_err(|e| e.to_string())?;

    if let Ok(head) = git(repo, &["rev-parse", "--verify", "-q", branch_ref.as_str()]) {
        return Ok(PhaseBranchResult {
            head,
            created: false,
        });
    }

    let zero = "0000000000000000000000000000000000000000";
    match git(
        repo,
        &["update-ref", branch_ref.as_str(), base_head.as_str(), zero],
    ) {
        Ok(_) => Ok(PhaseBranchResult {
            head: base_head,
            created: true,
        }),
        Err(_) => {
            let head = git(repo, &["rev-parse", branch_ref.as_str()]).map_err(|e| e.to_string())?;
            Ok(PhaseBranchResult {
                head,
                created: false,
            })
        }
    }
}

/// What a gate run produced: a result, or the fact that the caller cancelled
/// it before the command finished.
#[derive(Debug)]
pub(crate) enum GateRun {
    Done(GateResult),
    Cancelled,
}

/// The name a check must carry: short, no whitespace, no path or shell
/// characters, so a report line `[name]` stays unambiguous.
fn gate_check_name_ok(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// Reject an invalid check list before ANY check runs, so a bad list can never
/// produce a partial gate. Invalid input is `ErrorKind::InvalidInput`, which
/// [`gate_error`] reports as a non-retryable application failure.
fn validate_gate_input(input: &GateInput) -> std::io::Result<()> {
    if input.checks.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "gate has no checks",
        ));
    }
    let mut seen: HashSet<&str> = HashSet::new();
    for c in &input.checks {
        match c.builtin.as_deref() {
            Some("diffscan") => {
                let base = input.base_sha.as_deref().ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "diffscan requires base_sha",
                    )
                })?;
                pg_worker_core::git::check_rev(base).map_err(|_| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "invalid diffscan base_sha",
                    )
                })?;
            }
            Some("exports") | None => {}
            Some(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "unknown gate builtin",
                ));
            }
        }
        if !gate_check_name_ok(&c.name) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!(
                    "gate check name {name:?} must match [A-Za-z0-9_.-]{{1,64}}",
                    name = c.name
                ),
            ));
        }
        if !seen.insert(c.name.as_str()) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("duplicate gate check name {:?}", c.name),
            ));
        }
        if c.cmd.trim().is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("gate check {:?} has an empty command", c.name),
            ));
        }
        if c.timeout_s == Some(0) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("gate check {:?} timeout_s must be at least 1", c.name),
            ));
        }
    }
    Ok(())
}

/// Run the named checks of a card's gate sequentially, in list order, in one
/// confined directory, through the shared check runner.
///
/// Validation is up front ([`validate_gate_input`]) and the workdir is
/// confined before anything runs ([`pathjail::confine_dir`], whose escapes and
/// non-directories are `InvalidInput` as well): an empty list, a blank
/// command, a blank/duplicate/misnamed check, a `timeout_s` of 0 and a workdir
/// outside the card work root are invalid input, never a pass — and nothing
/// runs, so a bad list can never produce a partial gate. [`gate_error`] keeps
/// all of them non-retryable and the check runner is never entered with a bad
/// directory (it creates a sibling home dir).
///
/// Time is ONE shared budget of the configured `start_to_close` for the whole
/// gate: each check gets `min(its own timeout_s, what is left of the budget)`,
/// and a check that would start after the budget is spent is reported as not
/// run (`timed_out`, tail "not run: gate budget exhausted") instead of being
/// killed mid-flight.
///
/// Each check keeps its OWN bounded tail — the last `min(cfg.check_tail_lines,
/// 50)` lines, redacted and byte-capped by the check runner itself — and its
/// own `output_hash` of that tail. Nothing raw may reach Temporal history, and
/// hashing the raw output would let anyone with the hash confirm a guessed
/// secret (and would make the hash differ across runs that differ only in a
/// secret, breaking "identical failure" comparison). A failing check does not
/// stop the later ones: a later diff scan must still report.
///
/// Cancellation is unchanged: `cancelled` is polled by the check runner, which
/// kills the whole child process group, and it is checked between checks too;
/// the deadline is honoured in here because the blocking thread cannot be
/// cancelled from outside.
pub(crate) fn do_gate(
    cfg: &Config,
    input: &GateInput,
    cancelled: &dyn Fn() -> bool,
) -> std::io::Result<GateRun> {
    validate_gate_input(input)?;
    let dir = pathjail::confine_dir(Path::new(&input.workroot), &input.workdir)?;
    let started = Instant::now();
    let mut results = Vec::with_capacity(input.checks.len());
    for c in &input.checks {
        if cancelled() {
            return Ok(GateRun::Cancelled);
        }
        let remaining = cfg.start_to_close.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            // Do not start a command that must be killed mid-flight: report it
            // as not run, so the report still names every check of the gate.
            let tail = "not run: gate budget exhausted".to_string();
            results.push(GateCheckResult {
                name: c.name.clone(),
                passed: false,
                exit_code: None,
                signal: None,
                timed_out: true,
                output_hash: hash::hash_bytes(tail.as_bytes()),
                tail,
            });
            continue;
        }
        if let Some(builtin) = c.builtin.as_deref() {
            let (passed, reason) = if builtin == "diffscan" {
                let range = format!(
                    "{}..HEAD",
                    input.base_sha.as_deref().expect("validated base")
                );
                let diff = git(
                    &dir,
                    &[
                        "diff",
                        "--no-renames",
                        "--no-ext-diff",
                        "--no-textconv",
                        &range,
                        "--",
                    ],
                )
                .map_err(std::io::Error::other)?;
                let findings = pg_worker_core::diffscan::scan_diff(&diff);
                (
                    findings.is_empty(),
                    pg_worker_core::diffscan::reason(&findings),
                )
            } else {
                let missing = pg_worker_core::exports::verify(&input.exports, &|path| {
                    let candidate = dir.join(path);
                    let parent = candidate.parent()?.to_str()?;
                    let parent = pathjail::confine_dir(&dir, parent).ok()?;
                    let file = parent.join(candidate.file_name()?).canonicalize().ok()?;
                    if !file.starts_with(&dir) {
                        return None;
                    }
                    std::fs::read_to_string(file).ok()
                });
                let reason = if missing.is_empty() {
                    String::new()
                } else {
                    format!("missing exports: {}", missing.join(", "))
                };
                (missing.is_empty(), redact::cap_tail_bytes(&reason, 300))
            };
            let tail = redact::cap_tail_bytes(&redact::redact(&reason), redact::MAX_TAIL_BYTES);
            results.push(GateCheckResult {
                name: c.name.clone(),
                passed,
                exit_code: Some(if passed { 0 } else { 1 }),
                signal: None,
                timed_out: false,
                output_hash: hash::hash_bytes(tail.as_bytes()),
                tail,
            });
            continue;
        }
        let timeout = c
            .timeout_s
            .map(Duration::from_secs)
            .map_or(remaining, |t| t.min(remaining));
        // The runner redacts the WHOLE output before its line cut and caps the
        // bytes; the line cap per check is min(cfg lines, 50).
        let r = check::run_check_cancellable(
            &c.cmd,
            &dir,
            timeout,
            cfg.check_tail_lines.min(50),
            cancelled,
        )?;
        if r.cancelled {
            return Ok(GateRun::Cancelled);
        }
        // The tail is already redacted; the byte cap here is defensive only,
        // so a future runner change cannot push raw bytes into history.
        let tail = redact::cap_tail_bytes(&r.tail, redact::MAX_TAIL_BYTES);
        results.push(GateCheckResult {
            name: c.name.clone(),
            passed: r.passed(),
            exit_code: r.exit_code,
            signal: r.signal,
            timed_out: r.timed_out,
            output_hash: hash::hash_bytes(tail.as_bytes()),
            tail,
        });
    }
    Ok(GateRun::Done(GateResult {
        passed: results.iter().all(|c| c.passed),
        checks: results,
    }))
}

fn merge_result(outcome: &str, main_head: String, detail: String) -> MergeResult {
    MergeResult {
        outcome: outcome.to_string(),
        sha: None,
        main_head,
        detail,
    }
}

/// Files `branch` changed since `base`, judged against the write set.
fn confinement_violations(
    repo: &Path,
    base: &str,
    branch: &str,
    spec: &ConfineSpec,
) -> Result<Vec<confine::Violation>, mergelock::MergeError> {
    let range = format!("{base}..{branch}");
    let raw = git_bytes(repo, &["diff", "--name-only", "-z", "--no-renames", &range])?;
    let files: Vec<String> = raw
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect();
    confine::evaluate(files.iter().map(String::as_str), &spec.allow, &spec.forbid)
        .map_err(|e| mergelock::MergeError::Confine(e.to_string()))
}

/// Everything that happens under the keeper lock and the file lock.
fn merge_fenced(
    cfg: &Config,
    input: &MergeInput,
    fence: &Fence,
    started: Instant,
) -> Result<MergeResult, mergelock::MergeError> {
    let repo = Path::new(&input.repo);
    let main_ref = format!("refs/heads/{}", input.main_branch);
    let lock = mergelock::MergeLock::acquire(repo, MERGE_LOCK_TIMEOUT)?;
    let head_of_main = || git(repo, &["rev-parse", &main_ref]);

    // Fencing: the repo side remembers the highest token it admitted, so a
    // holder whose keeper lock expired cannot land a late merge — provided the
    // keeper itself issues strictly monotonic tokens (stub in tests; the real
    // lock endpoint is a RELEASE-DEPLOY gap). The token is
    // always the one the keeper granted this merge; a caller cannot name one.
    let token = fence.token;
    if !mergelock::admit_fence(&lock, repo, token)? {
        return Ok(merge_result(
            "merge_refused",
            head_of_main()?,
            format!("stale fencing token {token}"),
        ));
    }

    // Main moved since the attempt branched: rebase the attempt onto it
    // (inside its own worktree) instead of throwing the work away.
    let mut base = input.base_sha.clone();
    let wt = Path::new(&input.worktree);
    let head = head_of_main()?;
    let mut rebased = false;
    if head != base && !input.worktree.is_empty() && wt.is_dir() {
        if !mergelock::rebase_onto(wt, &head)? {
            return Ok(merge_result("conflict", head, String::new()));
        }
        base = head;
        rebased = true;
    }

    // The write set is judged again here, against what will really land.
    if let Some(spec) = &input.confine {
        let v = confinement_violations(repo, &base, &input.branch, spec)?;
        if !v.is_empty() {
            return Ok(merge_result(
                "contract_defect",
                head_of_main()?,
                format!("{v:?}"),
            ));
        }
    }

    // A rebased tree is code nobody ran: check it once before it lands.
    if let (true, Some(cmd)) = (rebased, input.check_cmd.as_deref()) {
        // The check gets what is left of the merge budget, not a fresh
        // start_to_close: the blocking thread cannot be cancelled from outside,
        // so the deadline has to be honoured in here.
        let left = merge_work_budget(cfg).saturating_sub(started.elapsed());
        if left.is_zero() {
            return Ok(merge_result(
                "check_fail",
                head_of_main()?,
                "merge budget exhausted before the post-rebase check".to_string(),
            ));
        }
        let r = check::run_check(cmd, wt, left, cfg.check_tail_lines)?;
        if !r.passed() {
            let tail = if r.timed_out {
                format!(
                    "post-rebase check exceeded the merge budget
{}",
                    r.tail
                )
            } else {
                r.tail
            };
            return Ok(merge_result("check_fail", head_of_main()?, tail));
        }
    }

    let outcome = mergelock::merge_locked(&lock, repo, &input.main_branch, &input.branch, &base)?;
    Ok(map_merge(outcome, head_of_main()?))
}

/// The keeper lock owner id of this worker process: pid plus a random suffix
/// drawn once at first use, so two workers on different hosts (same pid) or one
/// restarted worker never share an id. No host name goes into it or the logs.
pub(crate) fn merge_owner_id() -> &'static str {
    use std::hash::{BuildHasher, Hasher};
    static ID: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    ID.get_or_init(|| {
        // RandomState is seeded from the OS per instance; two draws give 128 bits.
        let draw = || {
            std::collections::hash_map::RandomState::new()
                .build_hasher()
                .finish()
        };
        format!(
            "pg-worker-{}-{:016x}{:016x}",
            std::process::id(),
            draw(),
            draw()
        )
    })
}

/// Merge `branch` into `main_branch`: keeper lock, then fencing + file lock,
/// then rebase / confinement / check as needed, then fast-forward. The keeper
/// lock is released on every path; a keeper that cannot grant it means
/// `retry_later`, never a merge without it. A refused release after the merge
/// landed does not undo it (the repo-side fence and file lock guarded the
/// update). Scope, stated honestly: the file lock plus the local CAS is the
/// exclusion that holds today, over one worker host per repo; the keeper side
/// only counts once a real keeper implements the lock endpoint.
pub(crate) fn do_merge(
    keeper: &dyn Keeper,
    cfg: &Config,
    input: &MergeInput,
) -> Result<MergeResult, mergelock::MergeError> {
    // The merge budget starts here, before the keeper lock is even asked for.
    let started = Instant::now();
    let repo = Path::new(&input.repo);
    let owner = merge_owner_id();
    let fence = match keeper.acquire_merge_lock(&input.repo, owner, merge_lock_ttl_s(cfg)) {
        Ok(f) => f,
        Err(e) => {
            let main_head = git(repo, &["rev-parse", &input.main_branch])?;
            return Ok(merge_result("retry_later", main_head, e.to_string()));
        }
    };
    // Released on every exit, including an early `?` or a panic in the merge.
    let _lease = KeeperLease {
        keeper,
        repo: &input.repo,
        fence,
    };
    merge_fenced(cfg, input, &_lease.fence, started)
}

/// The keeper-held merge lock, released on drop.
///
/// A refused release (stale token, keeper down) cannot undo or change what the
/// repo-side fence and file lock already decided, so it does not fail the
/// merge; it is logged, and the keeper's own ttl reclaims a lock it never saw
/// released.
struct KeeperLease<'a> {
    keeper: &'a dyn Keeper,
    repo: &'a str,
    fence: Fence,
}

impl Drop for KeeperLease<'_> {
    fn drop(&mut self) {
        if let Err(e) = self.keeper.release_merge_lock(self.repo, &self.fence) {
            eprintln!(
                "merge lock release for {} (token {}) failed: {e}",
                self.repo, self.fence.token
            );
        }
    }
}

/// Sweep dead attempts of `input.card` (every card when empty), treating none
/// as live, and report how many were removed. Every attempt branched from the same `base_sha`.
pub(crate) fn do_janitor(cfg: &Config, input: &JanitorInput) -> Result<u32, GitError> {
    let base = input.base_sha.clone();
    let base_for = move |_: &AttemptId| base.clone();
    let spec = janitor::ProbeSpec {
        check_cmd: &input.check_cmd,
        timeout: cfg.start_to_close,
        tail_lines: cfg.check_tail_lines,
        base_for: &base_for,
    };
    let live = HashSet::new();
    let repo = Path::new(&input.repo);
    let report = if input.card.is_empty() {
        janitor::sweep(repo, &live, &spec)?
    } else {
        janitor::sweep_card(repo, &input.card, &live, &spec)?
    };
    Ok(report.removed.len() as u32)
}

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
        input: AttemptInput,
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
                attempt::run_attempt_with(
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
        result.map_err(activity_error)
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
    /// threads in `spawn_blocking` behind an explicit 5 s timeout (G3-4), and
    /// `None` -- an unreachable keeper, a slow one, an unreadable or oversized
    /// body (G3-5) -- means "observed nothing": the workflow parks on its
    /// live-set fallback exactly as a run that could not read always did.
    // A short wire name, pinned by `gate_activity_name_and_payload_shapes_are_stable`
    // beside `attempt`; without the override the macro would emit
    // "CardActivities::observe" like `gate`/`probe`/`merge`.
    #[activity(name = "observe")]
    pub async fn observe(
        self: Arc<Self>,
        _ctx: ActivityContext,
        card: String,
    ) -> Result<Option<String>, ActivityError> {
        Ok(observe_read(self.deps.keeper.clone(), card).await)
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
        let key = input.key.clone();
        let branch = input.branch.clone();
        let head = input.head.clone();
        let res =
            tokio::task::spawn_blocking(move || do_phase_finish(&*keeper, &key, &branch, &head))
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
        let res = tokio::task::spawn_blocking(move || do_claim_card(&*keeper, &key))
            .await
            .map_err(activity_error)?;
        settle_refusal(res, "claim_card").map(Option::flatten)
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
            .map_err(|msg| {
                ActivityError::application(temporalio_sdk::ApplicationFailure::non_retryable(msg))
            })
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
    use super::*;
    use crate::harness::{Harness, HarnessRun};
    use crate::keeper::StubKeeper;
    use pg_worker_core::config::Tier;
    use pg_worker_core::worktree;
    use std::process::{Command, Stdio};
    use tempfile::TempDir;

    use crate::keeper::KeeperError;

    /// The packet the park-path tests park.
    fn g3_packet() -> ParkPacket {
        ParkPacket {
            card: "c-1".into(),
            tier: Tier::Free,
            exit_class: pg_worker_core::exit_class::ExitClass::UsageLimit,
            check_tail: "tail".into(),
        }
    }

    /// An execution identity built the way `park`/`ask_question` build one from
    /// their input: attempt from the input, `expected_state` from the input.
    fn g3_exec(expected_state: Option<Vec<String>>) -> ExecId {
        ExecId {
            workflow_id: Some("wf-1".into()),
            run_id: Some("run-1".into()),
            attempt: Some(4),
            expected_state,
        }
    }

    fn g3_live() -> Vec<String> {
        crate::keeper::WORKER_LIVE_STATES
            .iter()
            .map(|s| (*s).to_owned())
            .collect()
    }

    /// G3-1 + V7-5: the park path presents exactly the observation the
    /// workflow recorded in the input and never re-reads the keeper.
    #[test]
    fn park_forwards_the_recorded_observation_without_reading() {
        let input = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["waiting".into()]));
        // What the activity forwards (the whole of the park path's state use).
        assert_eq!(expected_state_of(&input), Some(vec!["waiting".to_string()]));
        let keeper = StubKeeper::default();
        // What the projector holds is what the workflow observed at decision
        // time; the park must present it without asking the keeper again.
        keeper.set_card_state(Some("waiting"));
        keeper
            .park(
                &g3_packet(),
                &g3_exec(expected_state_of(&input)),
            )
            .expect("park with its recorded observation lands");
        assert!(
            keeper.card_state_calls().is_empty(),
            "G3-1: the park path re-read the keeper"
        );
        assert_eq!(
            keeper.execs().last().expect("park was called").expected_state,
            Some(vec!["waiting".to_string()])
        );
        // No observation recorded: the workflow's live-set fallback stands.
        let plain = park_json_with_attempt(&g3_packet(), Some(4), Some(g3_live()));
        assert_eq!(expected_state_of(&plain), Some(g3_live()));
        assert_eq!(
            expected_state_of(&park_json_with_attempt(&g3_packet(), Some(4), None)),
            None
        );
    }

    /// G3-1 (red): a retried park carries the observation from decision time,
    /// so once the projector moved the card on, the keeper path refuses it
    /// (stub equivalent of `exec_stale_state`) instead of re-reading a fresher
    /// state that would make the CAS tautological.
    #[test]
    fn a_retried_park_with_a_stale_observation_is_refused() {
        let keeper = StubKeeper::default();
        // The card the run parked on was `running` when it decided; the
        // projector wrote `waiting` while the activity was delayed.
        keeper.set_card_state(Some("waiting"));
        let input = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["running".into()]));
        let err = keeper
            .park(&g3_packet(), &g3_exec(expected_state_of(&input)))
            .expect_err("stale observation must be refused");
        assert_eq!(err, KeeperError::Status(409));
        assert!(keeper.parked().is_empty());
        assert!(
            keeper.card_state_calls().is_empty(),
            "G3-1: a retry must not re-read to repair its expectation"
        );
        // The same park with the observation that still holds lands.
        keeper
            .set_card_state(Some("running"));
        keeper
            .park(&g3_packet(), &g3_exec(expected_state_of(&input)))
            .expect("fresh observation lands");
    }

    /// V7-5: the `park` activity's own code path -- [`park_path`], everything
    /// below the context binding the activity does -- forwards the observation
    /// it passes and never reads the keeper's card state (G3-1), on the
    /// landing case and on the refused one.
    #[tokio::test]
    async fn the_park_activity_path_presents_the_observation_it_passes() {
        let stub = Arc::new(StubKeeper::default());
        stub.set_card_state(Some("waiting"));
        let keeper: Arc<dyn Keeper> = stub.clone();
        // The identity the activity builds: observation verbatim from input.
        let landed = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["waiting".into()]));
        let exec = g3_exec(expected_state_of(&landed));
        park_path(keeper.clone(), landed, exec)
            .await
            .expect("park with its recorded observation lands");
        assert_eq!(stub.parked().len(), 1);
        assert!(
            stub.card_state_calls().is_empty(),
            "V7-5/G3-1: the park path read the keeper"
        );
        // The refused case runs the same code: stale observation, no re-read.
        let stale = park_json_with_attempt(&g3_packet(), Some(4), Some(vec!["running".into()]));
        let exec = g3_exec(expected_state_of(&stale));
        assert!(park_path(keeper.clone(), stale, exec).await.is_err());
        assert_eq!(stub.parked().len(), 1, "the stale park did not land");
        assert!(stub.card_state_calls().is_empty());
    }

    /// G3-3: an ask keeps the any-of live-set CAS the ADR documents -- it is
    /// never narrowed to a single state -- and re-reads nothing (G3-1).
    #[test]
    fn ask_keeps_the_any_of_live_set_and_does_not_read() {
        let keeper = StubKeeper::default();
        keeper.set_card_state(Some("running"));
        let input = AskInput {
            expected_state: Some(g3_live()),
            card: "c-1".into(),
            question: Question {
                text: "which db?".into(),
                default: Some("sqlite".into()),
                deadline_s: Some(60),
                risk_tags: Vec::new(),
            },
            attempt: Some(4),
        };
        // Exactly what the activity sends: the input set, verbatim.
        let exec = g3_exec(input.expected_state.clone());
        keeper
            .ask_question(&input.card, &input.question, &exec)
            .expect("ask lands");
        assert_eq!(
            keeper.execs().last().expect("ask was called").expected_state,
            Some(g3_live())
        );
        assert!(keeper.card_state_calls().is_empty());
    }

    /// G3-4: the one keeper read left waits a bounded time off the runtime
    /// threads, and a stalled keeper is "observed nothing".
    #[tokio::test]
    async fn observe_read_is_bounded_by_the_five_second_cap() {
        assert!(
            OBSERVE_READ_TIMEOUT <= Duration::from_secs(5),
            "G3-4: the read timeout must stay <= 5 s"
        );
        let stub = Arc::new(StubKeeper::default());
        stub.set_card_state(Some("waiting"));
        stub.set_card_state_delay(Some(Duration::from_millis(500)));
        let read: Arc<dyn Keeper> = stub.clone();
        let started = Instant::now();
        assert_eq!(
            observe_read_within(read, "c-1".into(), Duration::from_millis(25)).await,
            None,
            "a stalled read answers nothing"
        );
        assert!(started.elapsed() < Duration::from_secs(5));
        // An unstalled read hands the observation over.
        stub.set_card_state_delay(None);
        assert_eq!(
            observe_read(stub.clone(), "c-1".into()).await,
            Some("waiting".to_string())
        );
        assert_eq!(stub.card_state_calls(), vec!["c-1".to_string(), "c-1".to_string()]);
    }

    #[test]
    fn keeper_failures_are_classified() {
        for code in [401, 403, 409, 422] {
            assert_eq!(classify(&KeeperError::Status(code)), Failure::Refused);
        }
        for code in [408, 429, 500, 502, 503, 599] {
            assert_eq!(classify(&KeeperError::Status(code)), Failure::Transient);
        }
        assert_eq!(classify(&KeeperError::Http("x".into())), Failure::Transient);
        for code in [400, 404, 410] {
            assert_eq!(classify(&KeeperError::Status(code)), Failure::Other);
        }
        assert_eq!(classify(&KeeperError::Decode("x".into())), Failure::Other);
    }

    #[test]
    fn transient_failures_are_retried_then_succeed() {
        let mut calls = 0;
        let r = retry_transient(&[Duration::ZERO; 3], || {
            calls += 1;
            if calls < 3 {
                Err(KeeperError::Status(429))
            } else {
                Ok(7)
            }
        });
        assert_eq!((r, calls), (Ok(7), 3));
    }

    #[test]
    fn transient_failures_are_bounded_and_permanent_ones_are_not_retried() {
        let mut calls = 0;
        let r: Result<(), _> = retry_transient(&[Duration::ZERO; 3], || {
            calls += 1;
            Err(KeeperError::Status(503))
        });
        assert_eq!((r, calls), (Err(KeeperError::Status(503)), 4));
        for code in [401, 403, 409, 422, 400] {
            let mut calls = 0;
            let r: Result<(), _> = retry_transient(&[Duration::ZERO; 3], || {
                calls += 1;
                Err(KeeperError::Status(code))
            });
            assert_eq!((r, calls), (Err(KeeperError::Status(code)), 1), "{code}");
        }
    }

    #[test]
    fn only_a_permanent_refusal_settles_to_none() {
        for code in [401, 403, 409, 422] {
            assert!(matches!(
                settle_refusal::<i64>(Err(KeeperError::Status(code)), "v"),
                Ok(None)
            ));
        }
        for code in [408, 429, 500, 503, 400] {
            assert!(settle_refusal::<i64>(Err(KeeperError::Status(code)), "v").is_err());
        }
        assert!(matches!(settle_refusal(Ok(3), "v"), Ok(Some(3))));
    }

    /// A harness that is never run by these tests; it only satisfies `Deps`.
    struct Fake;

    impl Harness for Fake {
        fn name(&self) -> &str {
            "fake"
        }

        fn command(&self, _run: &HarnessRun) -> Command {
            Command::new("true")
        }
    }

    fn deps() -> Deps {
        Deps {
            keeper: Arc::new(StubKeeper::default()),
            harnesses: HarnessRegistry::new().with("opencode", Arc::new(Fake)),
            cfg: tiny(),
            patterns: Patterns::default(),
        }
    }

    // Exercise Activities::report itself: testing exec_of alone would not catch
    // replacing its call in report with ExecId::default(). No git fixture needed.
    #[temporalio_macros::workflow]
    #[derive(Default)]
    pub struct ReportIdentity;

    #[temporalio_macros::workflow_methods]
    impl ReportIdentity {
        #[run]
        pub async fn run(
            ctx: &mut temporalio_sdk::WorkflowContext<Self>,
            input: AttemptReport,
        ) -> temporalio_sdk::WorkflowResult<()> {
            ctx.execute_activity(
                CardActivities::report,
                input,
                temporalio_sdk::ActivityOptions::start_to_close_timeout(Duration::from_secs(10)),
            )
            .await?;
            Ok(())
        }
    }

    #[tokio::test]
    async fn report_carries_the_real_workflow_run_and_card_attempt() {
        use temporalio_client::{
            Client, ClientOptions, Connection, ConnectionOptions, Url, WorkflowFetchHistoryOptions,
            WorkflowGetResultOptions, WorkflowStartOptions,
        };
        use temporalio_sdk::{Runtime, Worker, WorkerOptions};
        let Ok(addr) = std::env::var("PG_TEMPORAL_ADDR") else {
            return;
        };
        if addr.is_empty() {
            return;
        }
        tokio::task::LocalSet::new().run_until(async move {
            let runtime = Runtime::from_current_tokio(Default::default()).expect("runtime");
            let conn = Connection::connect(ConnectionOptions::new(addr.parse::<Url>().unwrap()).build())
                .await.unwrap();
            let client = Client::new(conn, ClientOptions::new("default").build()).unwrap();
            let id = format!("report-identity-{}-{}", std::process::id(),
                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
            let keeper = Arc::new(StubKeeper::default());
            let mut d = deps();
            d.keeper = keeper.clone();
            let opts = WorkerOptions::new(id.as_str())
                .register_workflow::<ReportIdentity>().unwrap()
                .register_activities(CardActivities::new(d)).build();
            let mut worker = Worker::new(&runtime, client.clone(), opts).unwrap();
            let polling = tokio::task::spawn_local(async move { worker.run().await });
            let report = AttemptReport {
                card: "identity-card".into(), n: 7, leg: "leg".into(),
                exit_class: pg_worker_core::exit_class::ExitClass::Ok,
                diff_hash: "hash".into(), check_tail: String::new(),
            };
            client.start_workflow(ReportIdentity::run, report.clone(),
                WorkflowStartOptions::new(id.as_str(), id.as_str()).build()).await.unwrap();
            let handle = client.get_workflow_handle::<ReportIdentity>(id.as_str());
            tokio::time::timeout(Duration::from_secs(30),
                handle.get_result(WorkflowGetResultOptions::default())).await.unwrap().unwrap();
            let bytes = handle.fetch_history(WorkflowFetchHistoryOptions::default()).to_json().await.unwrap();
            polling.abort();
            let history: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            let run_id = history["events"][0]["attributes"]["WorkflowExecutionStartedEventAttributes"]
                ["first_execution_run_id"].as_str().expect("real server run id");
            assert!(!run_id.is_empty());
            assert_eq!(keeper.attempts(), vec![report]);
            assert_eq!(keeper.attempt_execs(), vec![ExecId {
                workflow_id: Some(id), run_id: Some(run_id.to_owned()),
                attempt: Some(7), expected_state: None,
            }]);
        }).await;
    }

    /// Defaults with the watchdogs shrunk so probes finish quickly.
    fn tiny() -> Config {
        Config {
            start_to_close: Duration::from_secs(10),
            check_tail_lines: 20,
            ..Config::default()
        }
    }

    fn claim() -> Claim {
        Claim {
            card: "c1".to_string(),
            repo: "/repo".to_string(),
            base_sha: "base".to_string(),
            check_cmd: "true".to_string(),
            allow: vec!["src/**".to_string()],
            forbid: vec![".env".to_string()],
            prompt: "do the thing".to_string(),
            ..Default::default()
        }
    }

    fn card_input() -> CardInput {
        CardInput {
            claim: claim(),
            legs: vec![LegSpec {
                id: "leg-1".to_string(),
                tier: Tier::Free,
                model: "some/model".to_string(),
                harness: "opencode".to_string(),
            }],
            workroot: "/w".to_string(),
            main_branch: "main".to_string(),
            question_wait_cap_s: None,
            max_question_rounds: None,
        }
    }

    fn round_trip<T>(v: &T)
    where
        T: Serialize + serde::de::DeserializeOwned + PartialEq + std::fmt::Debug,
    {
        let j = serde_json::to_string(v).unwrap();
        let back: T = serde_json::from_str(&j).unwrap();
        assert_eq!(&back, v);
    }

    #[test]
    fn serde_round_trip_of_wire_types() {
        round_trip(&card_input());
        round_trip(&CardOutput {
            outcome: "done".to_string(),
            detail: "merged".to_string(),
            merged_sha: Some("sha".to_string()),
            attempts: 2,
        });
        round_trip(&tick_now());
        round_trip(&ProbeInput {
            repo: "/r".to_string(),
            workroot: "/w".to_string(),
            card: "c1".to_string(),
            n: 1,
            base_sha: "b".to_string(),
            check_cmd: "true".to_string(),
        });
        round_trip(&ProbeResult {
            outcome: "already_done".to_string(),
            branch: "attempt/c1/1".to_string(),
        });
        round_trip(&MergeInput {
            repo: "/r".to_string(),
            main_branch: "main".to_string(),
            branch: "attempt/c1/1".to_string(),
            base_sha: "b".to_string(),
            worktree: "/w/c1-1".to_string(),
            check_cmd: Some("true".to_string()),
            confine: Some(ConfineSpec {
                allow: vec!["src/**".to_string()],
                forbid: vec![],
            }),
        });
        round_trip(&MergeResult {
            outcome: "merged".to_string(),
            sha: Some("sha".to_string()),
            main_head: "sha".to_string(),
            detail: String::new(),
        });
        round_trip(&DoneInput {
            expected_state: None,
            card: "c1".to_string(),
            merged_sha: "sha".to_string(),
            attempt: None,
        });
        round_trip(&JanitorInput {
            repo: "/r".to_string(),
            workroot: "/w".to_string(),
            check_cmd: "true".to_string(),
            base_sha: "b".to_string(),
            card: "c1".to_string(),
        });
        round_trip(&GateInput {
            base_sha: None,
            exports: Vec::new(),
            workroot: "/w".to_string(),
            workdir: "/w/c1-1".to_string(),
            checks: vec![GateCheck {
                builtin: None,
                name: "card-check".to_string(),
                cmd: "true".to_string(),
                timeout_s: Some(30),
            }],
        });
        round_trip(&GateResult {
            passed: false,
            checks: vec![GateCheckResult {
                name: "card-check".to_string(),
                passed: false,
                exit_code: Some(3),
                signal: None,
                timed_out: false,
                tail: "boom".to_string(),
                output_hash: "h".to_string(),
            }],
        });
    }

    /// An old producer's JSON predates `detail`; the missing field must default
    /// to empty rather than fail the read, so rolling upgrades stay compatible.
    #[test]
    fn card_output_without_detail_deserializes() {
        let old = r#"{"outcome":"done","merged_sha":"sha","attempts":1}"#;
        let out: CardOutput = serde_json::from_str(old).unwrap();
        assert_eq!(out.outcome, "done");
        assert_eq!(out.detail, "");
        assert_eq!(out.merged_sha.as_deref(), Some("sha"));
        assert_eq!(out.attempts, 1);
    }

    /// The park activity input carries the card attempt as an extra top-level
    /// key: `park_json_with_attempt` adds it, and `ParkPacket::from_json` still
    /// parses the result because it ignores unknown keys.
    #[test]
    fn park_json_with_attempt_carries_the_attempt_and_still_parses() {
        use pg_worker_core::exit_class::ExitClass;
        use std::collections::BTreeSet;
        let p = ParkPacket {
            card: "c1".to_string(),
            tier: Tier::Free,
            exit_class: ExitClass::CheckFail,
            check_tail: "boom".to_string(),
        };

        let tagged = park_json_with_attempt(&p, Some(4), Some(vec!["running".into()]));
        let any = park_json_with_attempt(
            &p,
            Some(4),
            Some(
                crate::keeper::WORKER_LIVE_STATES
                    .iter()
                    .map(|s| (*s).to_owned())
                    .collect(),
            ),
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&any).unwrap()["expected_state"],
            serde_json::json!(["open", "ready", "running", "waiting", "needs_input"])
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&tagged).unwrap()["expected_state"],
            "running"
        );
        let v: serde_json::Value = serde_json::from_str(&tagged).unwrap();
        assert_eq!(v["attempt"], 4);
        assert_eq!(
            v.as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from([
                "card",
                "tier",
                "exit_class",
                "check_tail",
                "attempt",
                "expected_state",
            ])
        );
        assert_eq!(ParkPacket::from_json(&tagged).unwrap(), p);
        // The activity reads the same key back out of the input.
        assert_eq!(park_attempt_of(&tagged), Some(4));

        let attempt_only = park_json_with_attempt(&p, Some(4), None);
        let v: serde_json::Value = serde_json::from_str(&attempt_only).unwrap();
        assert_eq!(
            v.as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["card", "tier", "exit_class", "check_tail", "attempt"])
        );
        assert_eq!(v["attempt"], 4);
        assert_eq!(ParkPacket::from_json(&attempt_only).unwrap(), p);
        assert_eq!(park_attempt_of(&attempt_only), Some(4));

        // Unset attempt: the plain packet JSON, no extra key at all.
        let plain = park_json_with_attempt(&p, None, None);
        let v: serde_json::Value = serde_json::from_str(&plain).unwrap();
        assert_eq!(
            v.as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["card", "tier", "exit_class", "check_tail"])
        );
        assert!(
            serde_json::from_str::<serde_json::Value>(&plain)
                .unwrap()
                .get("expected_state")
                .is_none()
        );
        assert_eq!(
            v,
            serde_json::from_str::<serde_json::Value>(&p.to_json()).unwrap()
        );
        assert_eq!(ParkPacket::from_json(&plain).unwrap(), p);
        assert_eq!(park_attempt_of(&plain), None);
    }

    #[test]
    fn tick_reports_wall_clock_and_unit_jitter() {
        for _ in 0..32 {
            let t = tick_now();
            assert!(t.jitter >= 0.0 && t.jitter < 1.0, "jitter {}", t.jitter);
            assert!(t.unix_ms > 0);
        }
    }

    #[test]
    fn map_probe_names_every_outcome() {
        let branch = || "attempt/c1/1".to_string();
        assert_eq!(
            map_probe(Ok(ProbeOutcome::AlreadyDone), branch()).outcome,
            "already_done"
        );
        assert_eq!(
            map_probe(Ok(ProbeOutcome::Partial), branch()).outcome,
            "partial"
        );
        assert_eq!(
            map_probe(Ok(ProbeOutcome::Nothing), branch()).outcome,
            "nothing"
        );
        let err = ProbeError::Git(GitError::BadRev("bad".to_string()));
        let r = map_probe(Err(err), branch());
        assert_eq!(r.outcome, "error");
        assert_eq!(r.branch, "attempt/c1/1");
    }

    #[test]
    fn map_merge_names_every_outcome() {
        let merged = map_merge(
            MergeOutcome::Merged {
                sha: "abc".to_string(),
            },
            "abc".to_string(),
        );
        assert_eq!(merged.outcome, "merged");
        assert_eq!(merged.sha.as_deref(), Some("abc"));
        assert_eq!(merged.main_head, "abc");

        let moved = map_merge(MergeOutcome::BaseMoved, "def".to_string());
        assert_eq!((moved.outcome.as_str(), moved.sha), ("base_moved", None));
        assert_eq!(moved.main_head, "def");

        let conflict = map_merge(MergeOutcome::Conflict, "def".to_string());
        assert_eq!(
            (conflict.outcome.as_str(), conflict.sha),
            ("conflict", None)
        );
    }

    #[test]
    fn card_activities_construct_from_cloned_deps() {
        let d = deps();
        let a = CardActivities::new(d.clone());
        // The clone proves `Deps: Clone`; the field reads prove the wiring landed.
        assert_eq!(a.deps.cfg.check_tail_lines, d.cfg.check_tail_lines);
        assert_eq!(a.deps.cfg.heartbeat, d.cfg.heartbeat);
    }

    /// Local git runner, pinned identity and hermetic env like attempt.rs tests.
    fn git(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .args([
                "-c",
                "user.name=tester",
                "-c",
                "user.email=t@example.com",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .current_dir(dir)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_TERMINAL_PROMPT", "0")
            .stdin(Stdio::null())
            .output()
            .expect("git runs");
        assert!(
            out.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }

    fn write(dir: &Path, rel: &str, body: &str) {
        std::fs::write(dir.join(rel), body).unwrap();
    }

    fn commit_all(dir: &Path, msg: &str) -> String {
        git(dir, &["add", "-A"]);
        git(dir, &["commit", "-q", "-m", msg]);
        git(dir, &["rev-parse", "HEAD"])
    }

    fn repo() -> (TempDir, String) {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path();
        git(p, &["init", "-q", "-b", "main"]);
        git(p, &["config", "commit.gpgsign", "false"]);
        write(p, "README.md", "hello\n");
        let sha = commit_all(p, "init");
        (dir, sha)
    }

    /// A branch one commit ahead of `base`, built in a scratch worktree so the
    /// caller's checkout stays on `main`.
    fn attempt_branch(p: &Path, name: &str, file: &str, body: &str, base: &str) -> String {
        let scratch = tempfile::tempdir().unwrap();
        let wt = scratch.path().join("wt");
        let wt_s = wt.to_string_lossy().into_owned();
        git(
            p,
            &[
                "worktree",
                "add",
                "-q",
                "-b",
                name,
                "--",
                wt_s.as_str(),
                base,
            ],
        );
        write(&wt, file, body);
        let head = commit_all(&wt, name);
        git(p, &["worktree", "remove", "--force", wt_s.as_str()]);
        head
    }

    fn probe_input(
        repo: &Path,
        workroot: &Path,
        n: u32,
        base: &str,
        check_cmd: &str,
    ) -> ProbeInput {
        ProbeInput {
            repo: repo.to_string_lossy().into_owned(),
            workroot: workroot.to_string_lossy().into_owned(),
            card: "c1".to_string(),
            n,
            base_sha: base.to_string(),
            check_cmd: check_cmd.to_string(),
        }
    }

    #[test]
    fn do_probe_sees_a_real_worktree() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        let wt = worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 1).dir_name()),
            &id,
        )
        .unwrap();
        write(&wt.path, "done.txt", "x");
        commit_all(&wt.path, "work");

        let input = probe_input(r.path(), root.path(), 1, &base, "test -f done.txt");
        let res = do_probe(&tiny(), &input);
        assert_eq!(res.outcome, "already_done");
        assert_eq!(res.branch, AttemptId::new("c1", 1).branch());

        // A worktree with nothing beyond base is `nothing`, even if dirty.
        let id2 = AttemptId::new("c1", 2);
        let wt2 = worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 2).dir_name()),
            &id2,
        )
        .unwrap();
        write(&wt2.path, "wip.txt", "x");
        let input2 = probe_input(r.path(), root.path(), 2, &base, "true");
        assert_eq!(do_probe(&tiny(), &input2).outcome, "nothing");
    }

    #[test]
    fn do_probe_reports_error_for_a_bad_base() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 1).dir_name()),
            &id,
        )
        .unwrap();

        let input = probe_input(r.path(), root.path(), 1, "deadbeef", "true");
        assert_eq!(do_probe(&tiny(), &input).outcome, "error");
    }

    fn merge_input(p: &Path, branch: &str, base: &str) -> MergeInput {
        MergeInput {
            repo: p.to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
            branch: branch.to_string(),
            base_sha: base.to_string(),
            worktree: String::new(),
            check_cmd: None,
            confine: None,
        }
    }

    fn merge(keeper: &StubKeeper, input: &MergeInput) -> MergeResult {
        do_merge(keeper, &tiny(), input).unwrap()
    }

    /// An attempt worktree with its branch checked out, one commit ahead of
    /// `base`. Returns the worktree path (inside `root`).
    fn attempt_worktree(
        p: &Path,
        root: &Path,
        card: &str,
        file: &str,
        body: &str,
        base: &str,
    ) -> std::path::PathBuf {
        let id = AttemptId::new(card, 1);
        let wt =
            worktree::create(p, base, &root.join(AttemptId::new(card, 1).dir_name()), &id).unwrap();
        write(&wt.path, file, body);
        commit_all(&wt.path, "attempt");
        wt.path
    }

    #[test]
    fn do_merge_fast_forwards_main() {
        let (r, base) = repo();
        let p = r.path();
        let head = attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        let keeper = StubKeeper::default();
        let res = merge(&keeper, &merge_input(p, "attempt/c1/1", &base));
        assert_eq!(res.outcome, "merged");
        assert_eq!(res.sha, Some(head.clone()));
        assert_eq!(res.main_head, head);
        // The keeper lock was taken and given back.
        assert!(!keeper.merge_lock_held());
        assert_eq!(keeper.lock_calls().len(), 2);
    }

    #[test]
    fn do_merge_reports_a_moved_base() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");
        // No worktree handed over: nothing to rebase, so the move is reported.
        let res = merge(
            &StubKeeper::default(),
            &merge_input(p, "attempt/c1/1", &base),
        );
        assert_eq!(res.outcome, "base_moved");
        assert_eq!(res.sha, None);
        assert_eq!(res.main_head, moved);
    }

    #[test]
    fn do_merge_reports_a_conflict() {
        let (r, base) = repo();
        let p = r.path();
        // An unrelated root commit shares no ancestry with main, so `--ff-only`
        // refuses instead of producing a merge commit.
        git(p, &["checkout", "-q", "--orphan", "attempt/orphan/1"]);
        commit_all(p, "orphan");
        git(p, &["checkout", "-q", "main"]);
        let res = merge(
            &StubKeeper::default(),
            &merge_input(p, "attempt/orphan/1", &base),
        );
        assert_eq!(res.outcome, "conflict");
        assert_eq!(res.sha, None);
        assert_eq!(res.main_head, base);
    }

    #[test]
    fn do_merge_without_the_keeper_lock_is_retry_later() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        let keeper = StubKeeper::default();
        keeper.set_lock_unavailable(true);
        let res = merge(&keeper, &merge_input(p, "attempt/c1/1", &base));
        assert_eq!(res.outcome, "retry_later");
        // Nothing merged.
        assert_eq!(git(p, &["rev-parse", "main"]), base);
    }

    #[test]
    fn merge_owner_id_is_stable_in_process_and_carries_a_random_suffix() {
        let a = merge_owner_id();
        assert_eq!(a, merge_owner_id());
        let rest = a
            .strip_prefix(&format!("pg-worker-{}-", std::process::id()))
            .expect(a);
        assert_eq!(rest.len(), 32);
        assert!(rest.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(&rest[..16], &rest[16..], "two independent draws");
    }

    #[test]
    fn do_merge_refuses_a_stale_fencing_token() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        attempt_branch(p, "attempt/c2/1", "z.txt", "z", &base);
        let keeper = StubKeeper::default();
        // A newer holder (token 2) merges first ...
        keeper.expire_merge_lock();
        let first = merge(&keeper, &merge_input(p, "attempt/c2/1", &base));
        assert_eq!(first.outcome, "merged");
        // ... then a holder whose keeper token is below the highest the repo
        // admitted is turned away (here the repo has seen a newer holder).
        let fence_file = git(p, &["rev-parse", "--git-common-dir"]);
        let fence_file = p.join(fence_file).join("pg-merge.fence");
        std::fs::write(&fence_file, "99").unwrap();
        let late = merge_input(p, "attempt/c1/1", &first.main_head);
        let res = merge(&keeper, &late);
        assert_eq!(res.outcome, "merge_refused");
        assert_eq!(git(p, &["rev-parse", "main"]), first.main_head);
        assert!(!keeper.merge_lock_held());
    }

    #[test]
    fn keeper_lease_releases_on_panic() {
        let keeper = StubKeeper::default();
        let fence = keeper.acquire_merge_lock("/r", "o", 60).unwrap();
        assert!(keeper.merge_lock_held());
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _lease = KeeperLease {
                keeper: &keeper,
                repo: "/r",
                fence,
            };
            panic!("merge blew up");
        }));
        assert!(r.is_err());
        assert!(!keeper.merge_lock_held());
    }

    #[test]
    fn merge_lock_ttl_covers_the_merge_activity_window() {
        let mut cfg = tiny();
        cfg.start_to_close = Duration::from_secs(900);
        assert!(merge_lock_ttl_s(&cfg) >= 900 + MERGE_LOCK_TIMEOUT.as_secs());
    }

    #[test]
    fn do_merge_rechecks_confinement_under_the_lock() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "secret.txt", "x", &base);
        let mut input = merge_input(p, "attempt/c1/1", &base);
        input.confine = Some(ConfineSpec {
            allow: vec!["src/**".to_string()],
            forbid: vec![],
        });
        let keeper = StubKeeper::default();
        let res = merge(&keeper, &input);
        assert_eq!(res.outcome, "contract_defect");
        assert!(res.detail.contains("secret.txt"), "{}", res.detail);
        assert_eq!(git(p, &["rev-parse", "main"]), base);
        assert!(!keeper.merge_lock_held());

        // Inside the write set it merges.
        input.confine = Some(ConfineSpec {
            allow: vec!["*.txt".to_string()],
            forbid: vec![],
        });
        assert_eq!(merge(&keeper, &input).outcome, "merged");
    }

    #[test]
    fn do_merge_rebases_when_main_moved_and_checks_once() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        // The check sees the rebased tree: both files must be present.
        input.check_cmd = Some("test -f x.txt && test -f y.txt".to_string());
        let res = merge(&StubKeeper::default(), &input);
        assert_eq!(res.outcome, "merged", "{res:?}");
        let sha = res.sha.unwrap();
        assert_eq!(git(p, &["rev-parse", "main"]), sha);
        assert_eq!(git(p, &["rev-parse", "main~1"]), moved);
    }

    #[test]
    fn do_merge_rebase_conflict_is_a_conflict() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "README.md", "attempt\n", &base);
        write(p, "README.md", "main\n");
        let moved = commit_all(p, "main edit");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        let res = merge(&StubKeeper::default(), &input);
        assert_eq!(res.outcome, "conflict");
        assert_eq!(git(p, &["rev-parse", "main"]), moved);
    }

    #[test]
    fn do_merge_failing_post_rebase_check_is_check_fail() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        input.check_cmd = Some("echo broke after rebase; exit 1".to_string());
        let keeper = StubKeeper::default();
        let res = merge(&keeper, &input);
        assert_eq!(res.outcome, "check_fail");
        assert!(res.detail.contains("broke after rebase"));
        assert_eq!(git(p, &["rev-parse", "main"]), moved);
        assert!(!keeper.merge_lock_held());
    }

    #[test]
    fn merge_budget_sits_inside_the_window_and_the_lock_ttl_covers_it() {
        for secs in [4u64, 900, 5400] {
            let mut cfg = tiny();
            cfg.start_to_close = Duration::from_secs(secs);
            assert!(merge_work_budget(&cfg) < merge_window(&cfg), "{secs}");
            assert!(merge_work_budget(&cfg) + merge_reserve(&cfg) <= merge_window(&cfg));
            assert!(merge_reserve(&cfg) <= Duration::from_secs(60));
            assert!(
                merge_lock_ttl_s(&cfg)
                    >= merge_window(&cfg).as_secs() + MERGE_LOCK_TIMEOUT.as_secs()
            );
        }
    }

    #[test]
    fn do_merge_post_rebase_check_honours_the_merge_deadline() {
        let (r, base) = repo();
        let p = r.path();
        let root = tempfile::tempdir().unwrap();
        attempt_worktree(p, root.path(), "c1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");

        let mut input = merge_input(p, &AttemptId::new("c1", 1).branch(), &base);
        input.worktree = root
            .path()
            .join(AttemptId::new("c1", 1).dir_name())
            .to_string_lossy()
            .into_owned();
        // A check far longer than the whole merge window.
        input.check_cmd = Some("sleep 120".to_string());
        let mut cfg = tiny();
        cfg.start_to_close = Duration::from_secs(4);
        let keeper = StubKeeper::default();
        let t = Instant::now();
        let res = do_merge(&keeper, &cfg, &input).unwrap();
        assert!(t.elapsed() < Duration::from_secs(20), "{:?}", t.elapsed());
        assert_eq!(res.outcome, "check_fail", "{res:?}");
        assert!(res.detail.contains("merge budget"), "{}", res.detail);
        // Nothing landed, and the keeper lock came back.
        assert_eq!(git(p, &["rev-parse", "main"]), moved);
        assert!(!keeper.merge_lock_held());
    }

    #[test]
    fn do_janitor_removes_dead_attempts() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        let wt = worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 1).dir_name()),
            &id,
        )
        .unwrap();
        write(&wt.path, "half.txt", "x");
        commit_all(&wt.path, "work");

        let input = JanitorInput {
            repo: r.path().to_string_lossy().into_owned(),
            workroot: root.path().to_string_lossy().into_owned(),
            check_cmd: "test -f other.txt".to_string(),
            base_sha: base,
            card: "c1".to_string(),
        };
        // Commit beyond base but a failing check is `partial`, so it is removed.
        assert_eq!(do_janitor(&tiny(), &input).unwrap(), 1);
        assert!(!wt.path.exists());
    }

    #[test]
    fn gate_builtin_validation_and_legacy_json() {
        let json =
            r#"{"workroot":".","workdir":".","checks":[{"name":"scan","cmd":"placeholder"}]}"#;
        let mut input: GateInput = serde_json::from_str(json).unwrap();
        assert_eq!(serde_json::to_string(&input).unwrap(), json);
        input.checks[0].builtin = Some("unknown".into());
        assert_eq!(
            do_gate(&tiny(), &input, &|| false).unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput
        );
        input.checks[0].builtin = Some("diffscan".into());
        assert_eq!(
            do_gate(&tiny(), &input, &|| false).unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn gate_diffscan_moved_test_is_a_deletion() {
        let (r, _) = repo();
        std::fs::create_dir(r.path().join("tests")).unwrap();
        std::fs::create_dir(r.path().join("docs")).unwrap();
        write(
            r.path(),
            "tests/test_a.py",
            "def test_a():\n    assert True\n",
        );
        let base = commit_all(r.path(), "test");
        git(r.path(), &["mv", "tests/test_a.py", "docs/check_a.py"]);
        commit_all(r.path(), "move test out of tests");
        let mut scan = named_check("scan", "exit 99", None);
        scan.builtin = Some("diffscan".into());
        let mut input = gate_input(r.path(), vec![scan]);
        input.base_sha = Some(base);
        let result = run(&tiny(), &input);
        assert!(!result.passed);
        assert!(
            result.checks[0]
                .tail
                .contains("deleted test file in tests/test_a.py")
        );
    }

    #[test]
    fn gate_diffscan_clean_deleted_test_and_later_check() {
        let (r, _) = repo();
        write(r.path(), "test_x.py", "def test_x():\n    assert True\n");
        let base = commit_all(r.path(), "test");
        let mut scan = named_check("scan", "exit 99", None);
        scan.builtin = Some("diffscan".into());
        let mut input = gate_input(r.path(), vec![scan, named_check("later", "true", None)]);
        input.base_sha = Some(base);
        assert!(run(&tiny(), &input).passed);
        std::fs::remove_file(r.path().join("test_x.py")).unwrap();
        commit_all(r.path(), "delete test");
        let result = run(&tiny(), &input);
        assert!(!result.passed);
        assert!(result.checks[0].tail.contains("deleted test"));
        assert_eq!(result.checks[0].name, "scan");
        assert!(result.checks[1].passed);
    }

    #[test]
    fn gate_exports_confines_files() {
        let (r, _) = repo();
        write(r.path(), "lib.rs", "pub fn run() {}");
        let mut check = named_check("exports", "exit 99", None);
        check.builtin = Some("exports".into());
        let mut input = gate_input(r.path(), vec![check]);
        input.exports = vec!["lib.rs:run".into()];
        assert!(run(&tiny(), &input).passed);
        input.exports.push("../lib.rs:run".into());
        let result = run(&tiny(), &input);
        assert!(!result.passed);
        assert!(result.checks[0].tail.contains("../lib.rs:run"));
    }

    #[test]
    fn do_janitor_scoped_to_one_card() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let c1 = AttemptId::new("c1", 1);
        let c2 = AttemptId::new("c2", 1);
        let wt1 = worktree::create(r.path(), &base, &root.path().join(c1.dir_name()), &c1).unwrap();
        let wt2 = worktree::create(r.path(), &base, &root.path().join(c2.dir_name()), &c2).unwrap();
        write(&wt1.path, "half.txt", "x");
        commit_all(&wt1.path, "work");

        let input = JanitorInput {
            repo: r.path().to_string_lossy().into_owned(),
            workroot: root.path().to_string_lossy().into_owned(),
            check_cmd: "test -f other.txt".to_string(),
            base_sha: base,
            card: "c1".to_string(),
        };

        // c1 is partial and removed; c2 belongs to another card and stays.
        assert_eq!(do_janitor(&tiny(), &input).unwrap(), 1);
        assert!(!wt1.path.exists());
        assert!(wt2.path.exists());
    }

    /// One named check for the gate tests.
    fn named_check(name: &str, cmd: &str, timeout_s: Option<u64>) -> GateCheck {
        GateCheck {
            builtin: None,
            name: name.to_string(),
            cmd: cmd.to_string(),
            timeout_s,
        }
    }

    /// A gate input that runs `checks` in `dir`, with `dir` as its own work root.
    fn gate_input(dir: &std::path::Path, checks: Vec<GateCheck>) -> GateInput {
        let workroot = dir.to_string_lossy().into_owned();
        GateInput {
            base_sha: None,
            exports: Vec::new(),
            workroot: workroot.clone(),
            workdir: workroot,
            checks,
        }
    }

    /// Run a gate that nobody cancels and take its result.
    fn run(cfg: &Config, input: &GateInput) -> GateResult {
        match do_gate(cfg, input, &|| false).unwrap() {
            GateRun::Done(result) => result,
            GateRun::Cancelled => panic!("a gate without a cancel must not report Cancelled"),
        }
    }

    #[test]
    fn gate_pass() {
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &tiny(),
            &gate_input(d.path(), vec![named_check("card-check", "echo fine", None)]),
        );
        assert!(r.passed);
        assert_eq!(r.checks.len(), 1);
        let c = &r.checks[0];
        assert_eq!(c.name, "card-check");
        assert!(c.passed && !c.timed_out);
        assert_eq!(c.exit_code, Some(0));
        assert_eq!(c.tail, "fine");
        assert!(!c.output_hash.is_empty());
    }

    #[test]
    fn gate_fail_keeps_tail_and_a_stable_hash() {
        let d = tempfile::tempdir().unwrap();
        let a = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![named_check("card-check", "echo nope; exit 3", None)],
            ),
        );
        let b = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![named_check("card-check", "echo nope; exit 3", None)],
            ),
        );
        assert!(!a.passed);
        let (ca, cb) = (&a.checks[0], &b.checks[0]);
        assert!(!ca.passed && !ca.timed_out);
        assert_eq!(ca.exit_code, Some(3));
        assert!(ca.tail.contains("nope"));
        assert_eq!(ca.output_hash, cb.output_hash);
    }

    #[test]
    fn two_checks_keep_separate_tails_and_hashes() {
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![
                    named_check("first", "echo one-a; echo one-b; exit 1", None),
                    named_check("second", "echo two-c", None),
                ],
            ),
        );
        // The gate fails because ONE check failed...
        assert!(!r.passed);
        assert_eq!(r.checks.len(), 2);
        let (a, b) = (&r.checks[0], &r.checks[1]);
        assert_eq!((a.name.as_str(), b.name.as_str()), ("first", "second"));
        assert!(!a.passed && b.passed);
        // ...but each check reports its own tail, with no bleed across them.
        assert!(
            a.tail.contains("one-a") && a.tail.contains("one-b"),
            "{}",
            a.tail
        );
        assert!(!a.tail.contains("two-c"), "{}", a.tail);
        assert!(b.tail.contains("two-c"), "{}", b.tail);
        assert!(!b.tail.contains("one-a"), "{}", b.tail);
        assert_ne!(a.output_hash, b.output_hash);
        assert_eq!(a.output_hash, hash::hash_bytes(a.tail.as_bytes()));
        assert_eq!(b.output_hash, hash::hash_bytes(b.tail.as_bytes()));
    }

    /// A failing first check must not stop the later ones: a later diff scan
    /// still reports, so the card sees everything the gate saw.
    #[test]
    fn a_failing_first_check_does_not_stop_the_later_ones() {
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![
                    named_check("fails", "exit 1", None),
                    named_check("still-runs", "echo done > ran-marker", None),
                ],
            ),
        );
        assert!(!r.passed);
        assert_eq!(r.checks.len(), 2);
        assert!(!r.checks[0].passed);
        assert!(r.checks[1].passed, "{}", r.checks[1].tail);
        assert!(
            d.path().join("ran-marker").exists(),
            "the later check must still have run"
        );
    }

    #[test]
    fn gate_budget_exhaustion_marks_later_checks_not_run() {
        let d = tempfile::tempdir().unwrap();
        // The first check eats the whole budget (it is killed at the budget's
        // end); the second must be reported as not run, never half-run.
        let cfg = Config {
            start_to_close: Duration::from_millis(500),
            ..tiny()
        };
        let input = gate_input(
            d.path(),
            vec![
                named_check("slow", "sleep 30", None),
                named_check("never", "echo too late", None),
            ],
        );
        let t = Instant::now();
        let r = run(&cfg, &input);
        assert!(t.elapsed() < Duration::from_secs(20), "{:?}", t.elapsed());
        assert!(!r.passed);
        assert_eq!(r.checks.len(), 2);
        assert!(
            r.checks[0].timed_out && !r.checks[0].passed,
            "{:?}",
            r.checks[0]
        );
        let second = &r.checks[1];
        assert!(second.timed_out && !second.passed, "{second:?}");
        assert_eq!(second.exit_code, None);
        assert_eq!(second.tail, "not run: gate budget exhausted");
        assert_eq!(second.output_hash, hash::hash_bytes(second.tail.as_bytes()));
    }

    #[test]
    fn gate_timeout_is_reported_and_capped_by_start_to_close() {
        let d = tempfile::tempdir().unwrap();
        let t = Instant::now();
        let r = run(
            &tiny(),
            &gate_input(
                d.path(),
                vec![named_check("card-check", "echo start; sleep 30", Some(1))],
            ),
        );
        assert!(!r.passed);
        assert!(r.checks[0].timed_out);
        assert!(r.checks[0].tail.contains("start"));
        assert!(t.elapsed() < Duration::from_secs(20));
        // A requested timeout above start_to_close is capped to it.
        let cfg = Config {
            start_to_close: Duration::from_secs(1),
            ..tiny()
        };
        let r = run(
            &cfg,
            &gate_input(
                d.path(),
                vec![named_check("card-check", "sleep 30", Some(3600))],
            ),
        );
        assert!(r.checks[0].timed_out && !r.passed);
    }

    /// Every invalid list is rejected before ANY check runs (so the marker
    /// file a check would create never appears) and is non-retryable input,
    /// never a pass and never a retry.
    #[test]
    fn gate_invalid_lists_run_nothing_and_are_not_retryable() {
        let d = tempfile::tempdir().unwrap();
        let marker_cmd = "echo ran > ran-marker";
        let cases: Vec<(&str, Vec<GateCheck>)> = vec![
            ("empty list", vec![]),
            (
                "duplicate name",
                vec![
                    named_check("dup", "true", None),
                    named_check("dup", marker_cmd, None),
                ],
            ),
            ("blank name", vec![named_check("", marker_cmd, None)]),
            (
                "name with a space",
                vec![named_check("bad name", marker_cmd, None)],
            ),
            (
                "name with a slash",
                vec![named_check("bad/name", marker_cmd, None)],
            ),
            (
                "name longer than 64",
                vec![named_check(&"x".repeat(65), marker_cmd, None)],
            ),
            ("blank command", vec![named_check("ok-name", "   ", None)]),
            (
                "zero timeout",
                vec![named_check("ok-name", marker_cmd, Some(0))],
            ),
            // A valid check before the invalid one must not run either: the
            // whole list is validated before the first command starts.
            (
                "valid first, invalid second",
                vec![
                    named_check("runs-first", marker_cmd, None),
                    named_check("bad name", "true", None),
                ],
            ),
        ];
        for (why, checks) in cases {
            let e = do_gate(&tiny(), &gate_input(d.path(), checks), &|| false).unwrap_err();
            assert_eq!(e.kind(), std::io::ErrorKind::InvalidInput, "{why}");
            assert!(!d.path().join("ran-marker").exists(), "{why} ran a check");
        }
        let ActivityError::Application(f) = gate_error(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "gate has no checks",
        )) else {
            panic!("expected an application failure")
        };
        assert!(f.is_non_retryable());
    }

    /// The workdir is confined to the workroot: anything that leaves it — a
    /// `..` component, an absolute path, a symlink out — is refused as invalid
    /// input before the check runner is entered.
    #[test]
    fn gate_workdir_escape_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("work");
        std::fs::create_dir(&root).unwrap();
        let outside = tmp.path().join("outside");
        std::fs::create_dir(&outside).unwrap();
        std::fs::create_dir(root.join("sub")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join("link")).unwrap();

        let workroot = root.to_string_lossy().into_owned();
        let outside = outside.to_string_lossy().into_owned();
        for candidate in [
            "../outside",
            "sub/../../outside",
            outside.as_str(),
            "link",
            "nope",
        ] {
            let input = GateInput {
                base_sha: None,
                exports: Vec::new(),
                workroot: workroot.clone(),
                workdir: candidate.to_string(),
                checks: vec![named_check("card-check", "true", None)],
            };
            let e = do_gate(&tiny(), &input, &|| false).unwrap_err();
            assert_eq!(e.kind(), std::io::ErrorKind::InvalidInput, "{candidate}");
            let ActivityError::Application(f) = gate_error(e) else {
                panic!("expected an application failure for {candidate}")
            };
            assert!(f.is_non_retryable(), "{candidate}");
        }
        // run_check_cancellable was never entered: it would have created this
        // sibling home dir for the missing subdir.
        assert!(!root.join("nope.home-check").exists());

        // A subdir of the root is inside the jail and runs normally.
        let input = GateInput {
            base_sha: None,
            exports: Vec::new(),
            workroot,
            workdir: "sub".to_string(),
            checks: vec![named_check("card-check", "test -d . && echo ok", None)],
        };
        let r = run(&tiny(), &input);
        assert!(r.passed, "{}", r.checks[0].tail);
        assert_eq!(r.checks[0].tail, "ok");
    }

    /// Fake secrets are built at runtime from fragments so that no token-shaped
    /// literal ever appears in this file.
    fn fake_sk(filler: &str) -> String {
        ["sk", "-", filler].concat()
    }

    fn fake_env(filler: &str) -> String {
        ["API_", "KEY=", filler].concat()
    }

    #[test]
    fn gate_tail_is_redacted_before_it_is_returned_and_hashed() {
        let d = tempfile::tempdir().unwrap();
        let (sk_a, sk_b) = (fake_sk(&"a".repeat(24)), fake_sk(&"b".repeat(24)));
        let (env_a, env_b) = (fake_env("hunter22hunter22"), fake_env("sleeper33sleeper3"));
        let cmd_a = format!("echo {sk_a}; echo {env_a}; exit 1");
        let cmd_b = format!("echo {sk_b}; echo {env_b}; exit 1");

        let a = run(
            &tiny(),
            &gate_input(d.path(), vec![named_check("card-check", &cmd_a, None)]),
        );
        let b = run(
            &tiny(),
            &gate_input(d.path(), vec![named_check("card-check", &cmd_b, None)]),
        );
        let (ta, tb) = (&a.checks[0].tail, &b.checks[0].tail);
        for secret in [sk_a.as_str(), sk_b.as_str(), env_a.as_str(), env_b.as_str()] {
            assert!(!ta.contains(secret) && !tb.contains(secret), "{ta}");
        }
        assert!(ta.contains(redact::REDACTED) && tb.contains(redact::REDACTED));
        // The hash is over the redacted tail, so two runs that differed only in
        // the secret compare equal — and the hash proves nothing about a guess.
        assert_eq!(ta, tb);
        assert_eq!(a.checks[0].output_hash, b.checks[0].output_hash);
        assert_eq!(a.checks[0].output_hash, hash::hash_bytes(ta.as_bytes()));
        assert_eq!(b.checks[0].output_hash, hash::hash_bytes(tb.as_bytes()));
    }

    #[test]
    fn gate_tail_is_capped_in_lines_and_bytes() {
        let cfg = Config {
            check_tail_lines: 50,
            ..tiny()
        };
        let d = tempfile::tempdir().unwrap();
        let r = run(
            &cfg,
            &gate_input(d.path(), vec![named_check("card-check", "seq 1 500", None)]),
        );
        let lines: Vec<&str> = r.checks[0].tail.lines().collect();
        assert_eq!(lines.len(), 50);
        assert_eq!(lines[0], "451");
        assert_eq!(lines[49], "500");

        let r = run(
            &cfg,
            &gate_input(
                d.path(),
                vec![named_check(
                    "card-check",
                    "head -c 200000 /dev/zero | tr '\\0' 'a'; echo",
                    None,
                )],
            ),
        );
        assert!(
            r.checks[0].tail.len() <= redact::MAX_TAIL_BYTES,
            "{}",
            r.checks[0].tail.len()
        );
    }

    /// True if Linux still exposes this pid as a live (non-zombie) process.
    fn pid_alive(pid: &str) -> bool {
        std::fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|s| {
            s.rsplit(')')
                .next()
                .and_then(|t| t.split_whitespace().next())
                != Some("Z")
        })
    }

    #[test]
    fn gate_cancel_kills_the_child() {
        let d = tempfile::tempdir().unwrap();
        let pid_file = d.path().join("child.pid");
        let cfg = Config {
            start_to_close: Duration::from_secs(30),
            ..tiny()
        };
        let input = gate_input(
            d.path(),
            vec![named_check(
                "card-check",
                "sleep 60 & echo $! > child.pid; wait",
                Some(30),
            )],
        );
        let flag = Arc::new(AtomicBool::new(false));
        let start = Instant::now();

        let watching = flag.clone();
        let handle =
            std::thread::spawn(move || do_gate(&cfg, &input, &|| watching.load(Ordering::SeqCst)));

        // Wait (up to 5 s) for the command to record the grandchild pid, then
        // cancel: the group must die well before its 30 s timeout.
        let mut seen = false;
        for _ in 0..500 {
            if pid_file.exists() {
                seen = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(seen, "the gate never recorded its child pid");
        flag.store(true, Ordering::SeqCst);

        let outcome = handle.join().unwrap();
        assert!(start.elapsed() < Duration::from_secs(15));
        assert!(
            matches!(outcome, Ok(GateRun::Cancelled)),
            "a cancelled gate must report GateRun::Cancelled"
        );

        let pid = std::fs::read_to_string(&pid_file).unwrap();
        let pid = pid.trim();
        if Path::new("/proc/self").exists() {
            let mut dead = false;
            for _ in 0..300 {
                if !pid_alive(pid) {
                    dead = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            assert!(dead, "backgrounded sleep {pid} is still alive");
        }
    }

    #[test]
    fn gate_runtime_io_error_stays_retryable() {
        let ActivityError::Application(f) = gate_error(std::io::Error::other("boom")) else {
            panic!("expected an application failure")
        };
        assert!(!f.is_non_retryable());
    }

    /// This test pins the generated activity names and the JSON shape of
    /// GateInput/GateResult. The register_activities call only proves
    /// registration does not panic: it cannot assert the registry contents,
    /// because ActivityDefinitions::names/get are pub(crate) in temporalio-sdk
    /// and there is no public accessor. Presence of `gate` in the registry is
    /// covered by the e2e worker test only if that test calls it (not yet).
    #[test]
    fn gate_activity_name_and_payload_shapes_are_stable() {
        assert_eq!(CardActivities::gate.name(), "CardActivities::gate");
        assert_eq!(CardActivities::observe.name(), "observe");
        assert_eq!(CardActivities::attempt.name(), "attempt");
        assert_eq!(CardActivities::probe.name(), "CardActivities::probe");
        assert_eq!(CardActivities::merge.name(), "CardActivities::merge");
        let mut defs = temporalio_sdk::activities::ActivityDefinitions::default();
        defs.register_activities(CardActivities::new(deps()));
        // `workroot`, `workdir` and `checks` are required on the wire; only a
        // check's `timeout_s` may be left out.
        let i: GateInput = serde_json::from_str(
            r#"{"workroot":"/w","workdir":"/w","checks":[{"name":"card-check","cmd":"true"}]}"#,
        )
        .unwrap();
        assert_eq!(i.checks.len(), 1);
        assert_eq!(i.checks[0].timeout_s, None);
        let json = serde_json::to_string(&i).unwrap();
        assert_eq!(
            json,
            r#"{"workroot":"/w","workdir":"/w","checks":[{"name":"card-check","cmd":"true"}]}"#
        );
        // A missing `checks` must fail the deserialisation, as a missing `cmd`
        // did before the list: a producer that cannot name its checks must not
        // pass as a silent no-op gate.
        assert!(serde_json::from_str::<GateInput>(r#"{"workroot":"/w","workdir":"/w"}"#).is_err());
        let r = GateResult {
            passed: true,
            checks: vec![GateCheckResult {
                name: "card-check".to_string(),
                passed: true,
                exit_code: Some(0),
                signal: None,
                timed_out: false,
                tail: "t".into(),
                output_hash: "h".into(),
            }],
        };
        assert_eq!(
            serde_json::to_string(&r).unwrap(),
            r#"{"passed":true,"checks":[{"name":"card-check","passed":true,"exit_code":0,"signal":null,"timed_out":false,"tail":"t","output_hash":"h"}]}"#
        );
        let back: GateResult = serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(back, r);
    }

    #[test]
    fn ensure_phase_branch_creates_and_is_idempotent() {
        let (dir, base) = repo();
        let path = dir.path().to_str().unwrap();
        let input = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "main".to_string(),
            branch: "phase/p1".to_string(),
        };

        let first = do_ensure_phase_branch(&input).unwrap();
        assert!(first.created);
        assert_eq!(first.head, base);
        assert_eq!(git(dir.path(), &["rev-parse", "refs/heads/phase/p1"]), base);

        git(dir.path(), &["commit", "--allow-empty", "-m", "advance"]);
        let second = do_ensure_phase_branch(&input).unwrap();
        assert!(!second.created);
        assert_eq!(second.head, base);
    }

    #[test]
    fn ensure_phase_branch_refuses_bad_input() {
        let (dir, _) = repo();
        let path = dir.path().to_str().unwrap();

        let same = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "phase/x".to_string(),
            branch: "phase/x".to_string(),
        };
        assert_eq!(
            do_ensure_phase_branch(&same).unwrap_err(),
            "phase branch must not be the base branch"
        );

        let not_phase = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "main".to_string(),
            branch: "main".to_string(),
        };
        assert_eq!(
            do_ensure_phase_branch(&not_phase).unwrap_err(),
            "not a phase/ branch"
        );

        let evil = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "-evil".to_string(),
            branch: "phase/p1".to_string(),
        };
        assert!(do_ensure_phase_branch(&evil).is_err());

        let missing = PhaseBranchInput {
            repo: path.to_string(),
            base_branch: "nope".to_string(),
            branch: "phase/p1".to_string(),
        };
        assert!(do_ensure_phase_branch(&missing).is_err());
    }

    #[test]
    fn phase_plan_pure_body_uses_keeper() {
        let keeper = StubKeeper::default();
        let plan: crate::phase::PhasePlan =
            serde_json::from_str(r#"{"phase":"p1","ready":true,"blocked":[],"cards":[]}"#).unwrap();
        keeper.push_plan(plan.clone());

        assert_eq!(do_phase_plan(&keeper, "p1").unwrap(), plan);
        assert_eq!(keeper.plan_calls(), vec!["p1".to_string()]);
    }

    #[test]
    fn finish_is_refusal_maps_400_and_refusal_statuses() {
        for status in [400, 401, 403, 409, 422] {
            assert!(finish_is_refusal(&crate::keeper::KeeperError::Status(
                status
            )));
        }
        for status in [404, 500, 503] {
            assert!(!finish_is_refusal(&crate::keeper::KeeperError::Status(
                status
            )));
        }
        assert!(!finish_is_refusal(&crate::keeper::KeeperError::Decode(
            "x".into()
        )));
    }

    #[test]
    fn phase_finish_pure_body_uses_keeper() {
        let keeper = StubKeeper::default();
        let finish = crate::phase::PhaseFinish {
            state: crate::phase::FINISH_MERGED.to_string(),
            pr: Some(crate::phase::PhasePr {
                number: 7,
                url: "u".to_string(),
            }),
            detail: String::new(),
        };
        keeper.push_finish(finish.clone());

        let got = do_phase_finish(&keeper, "p1", "phase/p1", "head1").unwrap();
        assert_eq!(got.state, finish.state);
        assert_eq!(
            got.pr.as_ref().map(|p| (p.number, p.url.as_str())),
            Some((7, "u"))
        );
        assert_eq!(got.detail, finish.detail);
        assert_eq!(
            keeper.finish_calls(),
            vec![(
                "p1".to_string(),
                "phase/p1".to_string(),
                "head1".to_string()
            )]
        );
    }

    #[test]
    fn claim_card_pure_body_uses_keeper() {
        let keeper = StubKeeper::default();
        let claim = Claim {
            card: "c1".to_string(),
            repo: "r".to_string(),
            base_sha: "s".to_string(),
            check_cmd: "true".to_string(),
            allow: vec![],
            forbid: vec![],
            prompt: "p".to_string(),
            ..Default::default()
        };
        keeper.push_card_claim(claim.clone());

        assert_eq!(do_claim_card(&keeper, "c1").unwrap(), Some(claim));
        assert_eq!(
            keeper.claim_card_calls(),
            vec![(merge_owner_id().to_string(), "c1".to_string())]
        );

        let empty = StubKeeper::default();
        assert_eq!(do_claim_card(&empty, "missing").unwrap(), None);
    }
}
