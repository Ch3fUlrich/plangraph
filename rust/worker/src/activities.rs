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
use crate::keeper::{Alarm, AttemptReport, Claim, Fence, Keeper};
use crate::mergelock::{self, MergeOutcome};
use crate::question::Question;
use pg_worker_core::budget;
use pg_worker_core::check;
use pg_worker_core::config::Config;
use pg_worker_core::confine;
use pg_worker_core::exit_class::Patterns;
use pg_worker_core::git::{GitError, git, git_bytes};
use pg_worker_core::janitor;
use pg_worker_core::packet::ParkPacket;
use pg_worker_core::probe::{self, ProbeError, ProbeOutcome};
use pg_worker_core::worktree::AttemptId;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use temporalio_macros::activities;
use temporalio_sdk::activities::{ActivityContext, ActivityError};

/// Task queue the card workflow and its activities share.
pub const TASK_QUEUE: &str = "pg-cards";

/// How long a worker waits for a competing merge before giving up.
const MERGE_LOCK_TIMEOUT: Duration = Duration::from_secs(30);

/// Seconds the keeper-held merge lock stays valid without a release.
///
/// The lock is taken before the file lock, so it must outlive the whole merge
/// activity: the wait for a competing merge ([`MERGE_LOCK_TIMEOUT`]) plus the
/// confinement recheck, rebase and post-rebase check, all of which the merge
/// activity's start-to-close timeout (`cfg.start_to_close`, see
/// `workflow::once_options`) already bounds. A fixed 120 s could expire under a
/// slow check and let a second merger in while this one was still running.
fn merge_lock_ttl_s(cfg: &Config) -> u64 {
    cfg.start_to_close.as_secs().max(1) + MERGE_LOCK_TIMEOUT.as_secs()
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

/// Ask the keeper to store an agent question for a card.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AskInput {
    pub card: String,
    pub question: Question,
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

/// The write set a merge re-checks under the lock.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConfineSpec {
    pub allow: Vec<String>,
    pub forbid: Vec<String>,
}

/// Fast-forward an attempt branch into main, guarded by the keeper lock, the
/// fencing token and the file lock. Every field after `base_sha` is optional so
/// a bare ff merge (and older recorded inputs) still deserialises.
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
    pub card: String,
    pub merged_sha: String,
}

/// Sweep dead attempts of one repo.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JanitorInput {
    pub repo: String,
    pub workroot: String,
    pub check_cmd: String,
    pub base_sha: String,
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
) -> Result<MergeResult, mergelock::MergeError> {
    let repo = Path::new(&input.repo);
    let main_ref = format!("refs/heads/{}", input.main_branch);
    let lock = mergelock::MergeLock::acquire(repo, MERGE_LOCK_TIMEOUT)?;
    let head_of_main = || git(repo, &["rev-parse", &main_ref]);

    // Fencing: the repo side remembers the highest token it admitted, so a
    // holder whose keeper lock expired cannot land a late merge. The token is
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
        let r = check::run_check(cmd, wt, cfg.start_to_close, cfg.check_tail_lines)?;
        if !r.passed() {
            return Ok(merge_result("check_fail", head_of_main()?, r.tail));
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
/// landed does not undo it (the repo-side fence already guarded the update).
pub(crate) fn do_merge(
    keeper: &dyn Keeper,
    cfg: &Config,
    input: &MergeInput,
) -> Result<MergeResult, mergelock::MergeError> {
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
    merge_fenced(cfg, input, &_lease.fence)
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

/// Sweep dead attempts of `repo`, treating none as live, and report how many
/// were removed. Every attempt branched from the same `base_sha`.
pub(crate) fn do_janitor(cfg: &Config, input: &JanitorInput) -> Result<u32, GitError> {
    let base = input.base_sha.clone();
    let base_for = move |_: &AttemptId| base.clone();
    let spec = janitor::ProbeSpec {
        check_cmd: &input.check_cmd,
        timeout: cfg.start_to_close,
        tail_lines: cfg.check_tail_lines,
        base_for: &base_for,
    };
    let report = janitor::sweep(Path::new(&input.repo), &HashSet::new(), &spec)?;
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
        _ctx: ActivityContext,
        input: AttemptReport,
    ) -> Result<(), ActivityError> {
        let keeper = self.deps.keeper.clone();
        tokio::task::spawn_blocking(move || keeper.report_attempt(&input))
            .await
            .map_err(activity_error)?
            .map_err(activity_error)
    }

    /// Park a card: the keeper wants a human to look at the packet.
    #[activity]
    pub async fn park(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: String,
    ) -> Result<(), ActivityError> {
        let keeper = self.deps.keeper.clone();
        tokio::task::spawn_blocking(move || {
            let packet =
                ParkPacket::from_json(&input).map_err(|e| activity_error(e.to_string()))?;
            keeper.park(&packet).map_err(activity_error)
        })
        .await
        .map_err(activity_error)?
    }

    /// Tell the keeper the card merged.
    #[activity]
    pub async fn complete(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: DoneInput,
    ) -> Result<(), ActivityError> {
        let keeper = self.deps.keeper.clone();
        tokio::task::spawn_blocking(move || keeper.complete(&input.card, &input.merged_sha))
            .await
            .map_err(activity_error)?
            .map_err(activity_error)
    }

    /// Raise an alarm for a condition only a human can clear.
    #[activity]
    pub async fn alarm(
        self: Arc<Self>,
        _ctx: ActivityContext,
        input: Alarm,
    ) -> Result<(), ActivityError> {
        let keeper = self.deps.keeper.clone();
        tokio::task::spawn_blocking(move || keeper.alarm(&input))
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
        _ctx: ActivityContext,
        input: AskInput,
    ) -> Result<Option<i64>, ActivityError> {
        let keeper = self.deps.keeper.clone();
        let res = tokio::task::spawn_blocking(move || {
            retry_transient(&TRANSIENT_BACKOFF, || {
                keeper.ask_question(&input.card, &input.question)
            })
        })
        .await
        .map_err(activity_error)?;
        settle_refusal(res, "ask_question")
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
        .await
        .map_err(activity_error)?;
        if let Err(e) = &res {
            eprintln!("record_question_timeout not recorded: {e}");
        }
        Ok(res.is_ok())
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
            card: "c1".to_string(),
            merged_sha: "sha".to_string(),
        });
        round_trip(&JanitorInput {
            repo: "/r".to_string(),
            workroot: "/w".to_string(),
            check_cmd: "true".to_string(),
            base_sha: "b".to_string(),
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
        };
        // Commit beyond base but a failing check is `partial`, so it is removed.
        assert_eq!(do_janitor(&tiny(), &input).unwrap(), 1);
        assert!(!wt.path.exists());
    }
}
