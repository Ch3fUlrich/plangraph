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
