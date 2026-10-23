//! One attempt, engine-free: fresh worktree, harness, confinement, check,
//! classification. No Temporal and no keeper calls here.
//!
//! Fail-closed at every seam: the sandbox is checked before and after the
//! harness, confinement is judged before the check is allowed to run, and a
//! failed check is re-run once unchanged so a flake is named `FlakyCheck`
//! instead of burning retry budget. The worktree is left in place — the janitor
//! probes and removes dead attempts, so nothing here destroys evidence.

use crate::harness::{self, Harness, HarnessRun, SandboxError, run_harness};
use crate::keeper::Claim;
use crate::opencode_events::{self, ProviderErrorKind};
use crate::question::{self, Question, Scan};
use pg_worker_core::check::{self, CheckResult};
use pg_worker_core::config::{Config, Tier};
use pg_worker_core::confine::{self, ConfineError, Violation};
use pg_worker_core::exit_class::{CheckOutcome, ExitClass, Patterns, Signals};
use pg_worker_core::git::{GitError, check_rev};
use pg_worker_core::stall::Opts;
use pg_worker_core::worktree::{self, AttemptId};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// One provider leg a card may run on: an id, its tier, and the model to hand
/// to the harness.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LegSpec {
    pub id: String,
    pub tier: Tier,
    pub model: String,
    /// Which agent CLI runs this leg: `opencode`, `claude` or `opencode-server`.
    /// Any model family can be a leg; Claude is one harness among several. Old
    /// histories without the field replay as `opencode`.
    #[serde(default = "default_harness")]
    pub harness: String,
}

/// The harness a leg uses when its spec does not name one.
pub fn default_harness() -> String {
    "opencode".to_string()
}

/// Everything one attempt needs to run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttemptInput {
    pub claim: Claim,
    pub n: u32,
    pub leg: LegSpec,
    /// Directory holding the attempt worktrees.
    pub workroot: String,
    /// Context window class text ("128k" | "256k" | "1m"); empty = unknown.
    #[serde(default)]
    pub class: String,
    /// Answers to earlier questions of this card, oldest first; appended to the
    /// prompt. Absent in old histories and while nothing was asked.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub answers: Vec<String>,
}

/// What one attempt produced.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttemptResult {
    pub exit_class: ExitClass,
    pub diff_hash: String,
    pub check_hash: String,
    pub check_tail: String,
    pub context_overflow: bool,
    pub branch: String,
    pub head: String,
    pub output_tail: String,
    /// The ten keys the Python projector reads (docs/projector.md). Each is
    /// absent from the JSON when unknown, so an old reader stays compatible.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub harness: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub combo: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub class: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens_in: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens_out: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cost: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub log_ref: Option<String>,
    /// The agent's question when the attempt ended `needs_input`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub question: Option<Question>,
}

/// Everything that can go wrong running an attempt.
#[derive(Debug, thiserror::Error)]
pub enum AttemptError {
    #[error(transparent)]
    Git(#[from] GitError),
    #[error("refusing worktree git directory: {0}")]
    GitDir(String),
    #[error(transparent)]
    Confine(#[from] ConfineError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

/// Run one attempt end to end and report how it ended.
///
/// `cancelled` is polled before any harness work (a cancelled attempt still
/// gets its fresh worktree so the caller's bookkeeping matches reality) and
/// again while the harness runs, so a cancel tears the agent down instead of
/// waiting for a stall or the 90-minute timeout.
pub fn run_attempt(
    h: &dyn Harness,
    cfg: &Config,
    patterns: &Patterns,
    inp: &AttemptInput,
    cancelled: &dyn Fn() -> bool,
) -> Result<AttemptResult, AttemptError> {
    run_attempt_with(Some(h), cfg, patterns, inp, cancelled)
}

/// [`run_attempt`] with the harness already looked up. `None` means the leg
/// names a harness this worker never registered: the attempt gets its worktree
/// (the probe after it expects one) and ends as a contract defect, because the
/// plan, not the provider, is wrong. The agent is never run.
pub fn run_attempt_with(
    h: Option<&dyn Harness>,
    cfg: &Config,
    patterns: &Patterns,
    inp: &AttemptInput,
    cancelled: &dyn Fn() -> bool,
) -> Result<AttemptResult, AttemptError> {
    // Stamped in one place so every caller's result carries the projector
    // keys, whether the attempt succeeded, was cancelled or was a defect.
    let started = std::time::SystemTime::now();
    let mut r = run_attempt_inner(h, cfg, patterns, inp, cancelled)?;
    r.harness = Some(h.map_or_else(|| inp.leg.harness.clone(), |h| h.name().to_string()));
    r.model = Some(inp.leg.model.clone());
    r.combo = Some(inp.leg.id.clone());
    r.class = if inp.class.is_empty() {
        None
    } else {
        Some(inp.class.clone())
    };
    r.started_at = Some(iso_utc(started));
    r.ended_at = Some(iso_utc(std::time::SystemTime::now()));
    Ok(r)
}

/// The attempt body: worktree, sandbox, harness, commit, confinement, check,
/// classification. `run_attempt` wraps this to stamp the result metadata.
fn run_attempt_inner(
    h: Option<&dyn Harness>,
    cfg: &Config,
    patterns: &Patterns,
    inp: &AttemptInput,
    cancelled: &dyn Fn() -> bool,
) -> Result<AttemptResult, AttemptError> {
    let claim = &inp.claim;
    let id = AttemptId::new(claim.card.clone(), inp.n);
    let branch = id.branch();
    let repo = Path::new(&claim.repo);
    let dest = Path::new(&inp.workroot).join(id.dir_name());

    // A retry or a promotion always starts from a fresh tree; an old one is
    // never reused.
    clear_dest(repo, &dest)?;
    let wt = worktree::create(repo, &claim.base_sha, &dest, &id)?;

    if cancelled() {
        return Ok(cancelled_result(&branch, &wt.head, String::new()));
    }

    if !matches!(claim.check_kind.as_str(), "" | "change" | "regression") {
        return defect_result(
            &dest,
            &claim.base_sha,
            &branch,
            &wt.head,
            format!(
                "unknown check_kind {:?}: expected change or regression",
                claim.check_kind
            ),
        );
    }

    // A link planted by the base that escapes the worktree is a contract
    // defect: a write through it lands beyond the write set.
    if let Some(paths) = sandbox_escape(&dest)? {
        return defect_result(
            &dest,
            &claim.base_sha,
            &branch,
            &wt.head,
            escape_tail(&paths),
        );
    }

    let Some(h) = h else {
        return defect_result(
            &dest,
            &claim.base_sha,
            &branch,
            &wt.head,
            format!(
                "unknown harness {:?} on leg {}: not registered with this worker",
                inp.leg.harness, inp.leg.id
            ),
        );
    };

    let prompt = question::prompt_with_answers(&claim.prompt, &inp.answers);
    let run = HarnessRun {
        workdir: dest.as_path(),
        model: inp.leg.model.as_str(),
        prompt: prompt.as_str(),
    };
    if let Err(why) = h.validate(&run) {
        return defect_result(&dest, &claim.base_sha, &branch, &wt.head, why);
    }
    // The base check is untrusted card code too: use the same jail and private
    // tmp isolation as the harness and post-attempt check, even if it parks early.
    let tmp = attempt_tmp()?;
    let jail = crate::harness::JailConfig::from_env()
        .map_err(std::io::Error::other)?
        .map(|mut jail| {
            jail.tmp = tmp.path().to_path_buf();
            jail
        });
    if claim.check_kind != "regression" && !claim.check_cmd.trim().is_empty() {
        // Base-check state must never be visible to the writer.
        let base_tmp = attempt_tmp()?;
        // Capture the pristine diff before the check can create scratch files.
        let base_diff_hash = confine::diff_hash(&dest, &claim.base_sha)?;
        let pre = check::run_check_scoped(
            &claim.check_cmd,
            &dest,
            cfg.start_to_close,
            cfg.check_tail_lines,
            jail.as_ref()
                .map(|j| (j.bin.as_path(), &j.limits, base_tmp.path())),
            &|| false,
        );
        // Base-check scratch is not writer work, even when the check parks.
        worker_git(&dest, &["checkout", "--", "."])?;
        worker_git(&dest, &["clean", "-fdxq"])?;
        drop(base_tmp);
        let pre = pre?;
        if pre.passed() {
            let mut result = cancelled_result(&branch, &wt.head, String::new());
            result.exit_class = ExitClass::ContractDefect;
            result.diff_hash = base_diff_hash;
            result.check_tail = "red-first: check already passes on base sha (set check_kind=regression if intended)".into();
            return Ok(result);
        }
    }
    let opts = Opts {
        stall: Some(cfg.stall),
        timeout: Some(cfg.start_to_close),
        grace: cfg.kill_grace,
        max_output: 1 << 20,
    };
    // The agent's private HOME lives beside the worktree (never in its diff) and
    // goes away with the attempt. The check below gets its own HOME, so a login
    // the harness persisted here never reaches the check child.
    let mut home = pg_worker_core::sandbox_env::HomeGuard::for_dir(&dest)?;
    // Harness auth travels as staged files in this HOME, never by env (the
    // confined env carries no credential names; see sandbox_env). The guard
    // removes what it staged when the attempt ends.
    home.stage_harness_auth()?;
    let sup = run_harness(h, &run, home.path(), &opts, cancelled, jail.as_ref())?;
    let output = String::from_utf8_lossy(&sup.output).into_owned();
    let output_tail = check::tail_lines(&output, cfg.check_tail_lines);

    // A cancel that arrived while the harness ran kills the agent, so nothing
    // it wrote is committed and the commit/confinement/check stages are
    // skipped: the caller only needs to know the attempt was cancelled.
    if sup.cancelled {
        return Ok(cancelled_result(&branch, &wt.head, output_tail));
    }

    let clean = sup.code == Some(0) && !sup.stalled && !sup.timed_out;

    // A JSON-emitting harness says more than its exit code can: a provider
    // error is the provider's fault whatever the code says, and an empty
    // stream means the run never got going.
    let ev = h.json_events().then(|| opencode_events::summarize(&output));
    let provider_rate_limited = ev.as_ref().is_some_and(|e| {
        e.provider_error
            .as_ref()
            .is_some_and(|p| p.kind == ProviderErrorKind::RateLimit)
    });
    let provider_error = ev.as_ref().is_some_and(|e| {
        e.provider_error
            .as_ref()
            .is_some_and(|p| p.kind == ProviderErrorKind::Other)
    });
    let empty_output = clean && ev.as_ref().is_some_and(|e| e.is_empty_run());
    // Anything the provider got wrong skips the check: the check would judge
    // work the run never did.
    let provider_failed = provider_rate_limited || provider_error || empty_output;

    // A question packet on a run that was not killed is a request for input; an
    // unusable packet is a defect of the harness contract, not something to retry.
    let (question, malformed) = if sup.stalled || sup.timed_out {
        (None, None)
    } else {
        match question::scan_output(&output) {
            Scan::None => (None, None),
            Scan::Asked(q) => (Some(q), None),
            Scan::Malformed(why) => (None, Some(why)),
        }
    };

    // The harness itself must not have left an escaping link behind.
    if let Some(paths) = sandbox_escape(&dest)? {
        return defect_result(
            &dest,
            &claim.base_sha,
            &branch,
            &wt.head,
            escape_tail(&paths),
        );
    }

    // Nothing is staged, committed or hashed until the worktree's own git
    // directory has proved it is still the one git made: a `.git` the harness
    // rewrote would send every git call below — and every hash taken from it —
    // to some other repository.
    verify_git_dir(&dest, repo, &claim.base_sha)?;

    // Whether the work changed decides both the commit and `NoChange`, so it
    // is read once from git rather than inferred later.
    let mut changed = false;
    if clean {
        worker_git(&dest, &["add", "-A"])?;
        // `--quiet` implies `--exit-code`: a non-zero exit means something is
        // staged. A clean tree must not create an empty commit.
        let staged = worker_git(&dest, &["diff", "--cached", "--quiet"]).is_err();
        // The harness may have committed its own work: the index then matches
        // HEAD with nothing staged, but the tree sits past the claim's base,
        // so the staged diff alone would miss it and call the run `NoChange`.
        let committed = worker_git(&dest, &["rev-parse", "HEAD"])? != claim.base_sha;
        if staged {
            worker_git(
                &dest,
                &[
                    "-c",
                    "user.name=pg-worker",
                    "-c",
                    "user.email=worker@localhost",
                    "commit",
                    "-q",
                    "-m",
                    &format!("attempt {}#{}", claim.card, inp.n),
                ],
            )?;
        }
        changed = staged || committed;
    }
    let head = worker_git(&dest, &["rev-parse", "HEAD"])?;

    // Confinement is judged before the check: a write outside the set is a
    // contract defect however the check would have gone, so the check is
    // skipped entirely.
    let mut forbid = claim.forbid.clone();
    // Reuse confinement's matcher, including directory prefixes and literal
    // separators, rather than maintaining another interpretation of allow.
    for violation in confine::evaluate(
        claim.check_reads.iter().map(String::as_str),
        &claim.allow,
        &[],
    )? {
        if let Violation::OutsideWriteSet { path } = violation {
            forbid.push(path);
        }
    }
    let violations = confine::check_confinement(&dest, &claim.base_sha, &claim.allow, &forbid)?;
    let contract = !violations.is_empty() || malformed.is_some();

    // A clean run that changed nothing — neither staged nor committed past the
    // base — did not do the work. It is `NoChange` for every harness, not only
    // the JSON ones, so it comes from git; a question or a defect already
    // explains the run better.
    let no_change = clean && !changed && question.is_none() && !contract && !provider_failed;

    // Hashed before the check so a check's own scratch files never pollute the
    // diff of what the attempt changed.
    let diff_hash = confine::diff_hash(&dest, &claim.base_sha)?;
    // A provider failure or a no-op run never reaches the check: either way the
    // check would judge work the run never attempted.
    let check_allowed = clean && !contract && question.is_none() && !provider_failed && !no_change;
    let (outcome, check_hash, check_tail) = if check_allowed {
        let (o, last) = check_with_rerun(&claim.check_cmd, &dest, cfg, jail.as_ref())?;
        (Some(o), last.output_hash, last.tail)
    } else if let Some(why) = &malformed {
        (None, String::new(), why.clone())
    } else {
        (None, String::new(), violations_tail(&violations))
    };

    let classification = patterns.classify(&Signals {
        exit_code: sup.code,
        signal: sup.signal,
        check: outcome,
        provider_rate_limited,
        provider_error,
        empty_output,
        no_change,
        timed_out: sup.timed_out,
        stalled: sup.stalled,
        contract_defect: contract,
        needs_input: question.is_some(),
        stderr: output,
        cancelled: sup.cancelled,
        ..Signals::default()
    });

    Ok(AttemptResult {
        exit_class: classification.class,
        diff_hash,
        check_hash,
        check_tail,
        context_overflow: classification.context_overflow,
        branch,
        head,
        output_tail,
        harness: None,
        model: None,
        combo: None,
        class: None,
        started_at: None,
        ended_at: None,
        tokens_in: ev.as_ref().and_then(|e| e.tokens_in),
        tokens_out: ev.as_ref().and_then(|e| e.tokens_out),
        cost: None,
        log_ref: None,
        question: question.filter(|_| classification.class == ExitClass::NeedsInput),
    })
}

/// Map a finished attempt onto the retry budget's input shape.
pub fn to_budget_attempt(r: &AttemptResult, leg: &LegSpec) -> pg_worker_core::budget::Attempt {
    pg_worker_core::budget::Attempt {
        exit_class: r.exit_class,
        leg: leg.id.clone(),
        tier: leg.tier,
        diff_hash: r.diff_hash.clone(),
        check_hash: r.check_hash.clone(),
        context_overflow: r.context_overflow,
    }
}

/// Format `t` as RFC 3339 UTC with millisecond precision, e.g.
/// `2026-10-16T03:17:55.123Z`. Times before the epoch clamp to the epoch, so
/// a skewed clock never yields a negative date.
pub fn iso_utc(t: std::time::SystemTime) -> String {
    let d = t.duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    let secs = d.as_secs();
    let millis = d.subsec_millis();
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (y, m, day) = civil_from_days(days);
    let (hh, mm, ss) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    format!("{y:04}-{m:02}-{day:02}T{hh:02}:{mm:02}:{ss:02}.{millis:03}Z")
}

/// Proleptic-Gregorian days-since-1970 to civil (year, month, day) by Howard
/// Hinnant's `civil_from_days`; avoids a date dependency for one format.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `Some(paths)` when `dest` holds symlinks resolving outside it.
fn sandbox_escape(dest: &Path) -> Result<Option<Vec<PathBuf>>, AttemptError> {
    match harness::check_sandbox(dest) {
        Ok(()) => Ok(None),
        Err(SandboxError::Escape(paths)) => Ok(Some(paths)),
        Err(SandboxError::Io(e)) => Err(AttemptError::Io(e)),
    }
}

/// Name the offending paths so a human sees exactly which link escaped.
fn escape_tail(paths: &[PathBuf]) -> String {
    let names: Vec<String> = paths.iter().map(|p| p.display().to_string()).collect();
    format!("sandbox escape: {}", names.join(", "))
}

/// Describe confinement violations as the tail the planner will read.
fn violations_tail(violations: &[Violation]) -> String {
    violations
        .iter()
        .map(|v| match v {
            Violation::OutsideWriteSet { path } => format!("outside write set: {path}"),
            Violation::Forbidden { path } => format!("forbidden: {path}"),
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// A cancelled attempt: keep the branch and head the caller already knows and
/// skip the commit, confinement and check stages. `output_tail` carries what
/// the harness printed before it was killed.
fn cancelled_result(branch: &str, head: &str, output_tail: String) -> AttemptResult {
    AttemptResult {
        exit_class: ExitClass::Cancelled,
        diff_hash: String::new(),
        check_hash: String::new(),
        check_tail: String::new(),
        context_overflow: false,
        branch: branch.to_string(),
        head: head.to_string(),
        output_tail,
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

/// A contract-defect result, keeping the diff so the planner can still tell
/// whether anything was written.
fn defect_result(
    dest: &Path,
    base: &str,
    branch: &str,
    head: &str,
    output_tail: String,
) -> Result<AttemptResult, AttemptError> {
    Ok(AttemptResult {
        exit_class: ExitClass::ContractDefect,
        diff_hash: confine::diff_hash(dest, base)?,
        check_hash: String::new(),
        check_tail: String::new(),
        context_overflow: false,
        branch: branch.to_string(),
        head: head.to_string(),
        output_tail,
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
    })
}

/// Run the check; on failure run it once more unchanged. A pass on the second
/// run is a flake, not a fix. Returns the outcome and the last run's result.
fn check_with_rerun(
    cmd: &str,
    dest: &Path,
    cfg: &Config,
    jail: Option<&crate::harness::JailConfig>,
) -> Result<(CheckOutcome, CheckResult), AttemptError> {
    let jail = jail.map(|j| (j.bin.as_path(), &j.limits, j.tmp.as_path()));
    let first = check::run_check_scoped(
        cmd,
        dest,
        cfg.start_to_close,
        cfg.check_tail_lines,
        jail,
        &|| false,
    )?;
    if first.passed() {
        return Ok((CheckOutcome::Pass, first));
    }
    let second = check::run_check_scoped(
        cmd,
        dest,
        cfg.start_to_close,
        cfg.check_tail_lines,
        jail,
        &|| false,
    )?;
    let outcome = if second.passed() {
        CheckOutcome::Flaky
    } else {
        CheckOutcome::Fail
    };
    Ok((outcome, second))
}

/// Remove a stale worktree before a fresh `worktree add`. A directory git no
/// longer knows as a worktree is deleted directly (the "not found" case); any
/// other git error is real and propagates.
fn clear_dest(repo: &Path, dest: &Path) -> Result<(), AttemptError> {
    if !dest.exists() {
        return Ok(());
    }
    match worktree::remove(repo, dest) {
        Ok(()) => Ok(()),
        Err(GitError::Failed { stderr, .. }) if stderr.contains("not a working tree") => {
            std::fs::remove_dir_all(dest)?;
            Ok(())
        }
        Err(e) => Err(e.into()),
    }
}

// Never grant an attempt write access to the shared system temp root.
fn attempt_tmp() -> std::io::Result<tempfile::TempDir> {
    tempfile::Builder::new().prefix("pg-attempt-").tempdir()
}

/// Environment names a worker-side git child may inherit. Everything else in
/// the worker's own process environment is dropped: every `PG_*` credential, a
/// `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` override, an injected
/// `GIT_CONFIG_*` — none of it reaches a git process.
const GIT_ENV_PASS: &[&str] = &["PATH", "LANG", "TERM"];

/// Every git call this module makes: `git [flags] <args>` in `dir`.
///
/// * the environment is scrubbed down to [`GIT_ENV_PASS`] (no `PG_*`), so a
///   credential in the worker's environment is invisible to git;
/// * `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`, so neither the
///   machine's nor the user's config extends or redirects the command;
/// * `core.hooksPath=/dev/null` before every subcommand and `--no-verify` on
///   every `commit`, so no hook planted in the repository ever runs as the
///   worker — a hook is code from the worktree, not from this plan.
fn git_command(dir: &Path, args: &[&str]) -> Command {
    let mut cmd = Command::new("git");
    cmd.env_clear();
    for name in GIT_ENV_PASS {
        if let Some(v) = std::env::var_os(name) {
            cmd.env(name, v);
        }
    }
    cmd.env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_TERMINAL_PROMPT", "0")
        .current_dir(dir)
        .stdin(Stdio::null())
        .arg("-c")
        .arg("core.hooksPath=/dev/null");
    for a in args {
        cmd.arg(*a);
        if *a == "commit" {
            cmd.arg("--no-verify");
        }
    }
    cmd
}

/// Run `git <args>` in `dir` under [`git_command`] and return trimmed stdout.
fn worker_git(dir: &Path, args: &[&str]) -> Result<String, GitError> {
    let out = git_command(dir, args).output()?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    } else {
        Err(GitError::Failed {
            args: args.join(" "),
            code: out.status.code(),
            stderr: String::from_utf8_lossy(&out.stderr).trim().to_string(),
        })
    }
}

/// `path` as git reports it: canonical when it can be resolved, lexical
/// otherwise. Comparisons built on it are fail-closed — a path that will not
/// resolve compares unequal to anything it merely resembles.
fn canonical(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// The worktree's own git directory, judged immediately before anything is
/// staged.
///
/// Everything after this call — the add, the commit, the diff hash,
/// confinement — runs through git in `dest`, so a `.git` the harness rewrote
/// would send all of it, and every hash taken from it, to some other
/// repository. Checked here:
///
/// * `.git` at the worktree root is a file (the gitfile `git worktree add`
///   writes) or a directory — never a symlink, which only a harness could
///   have planted, and never anything else;
/// * git resolves the git directory into the claim repository's own worktree
///   storage, `<common dir>/worktrees/…` — the one place `git worktree add`
///   registers a linked worktree, and the only place [`worktree::create`]
///   creates one. "Under `dest`" or "under `repo`" is deliberately not enough:
///   a harness that deletes the gitfile and runs `git init` gets a real
///   `.git` directory under `dest` (or a gitfile pointing under `dest` or at
///   `repo/.git`), which passes those looser tests while making git operate on
///   a repository the harness chose — its base, ancestry and history, however
///   they were grafted together;
/// * git sees `dest` as the top of the worktree it is standing in, and
///   `HEAD` still descends from the claim's `base`, so the commit below
///   extends the branch that was claimed rather than some other line.
///
/// Any failure fails the attempt: nothing is staged on a guess.
fn verify_git_dir(dest: &Path, repo: &Path, base: &str) -> Result<(), AttemptError> {
    let refuse = |why: &str| AttemptError::GitDir(format!("{}: {why}", dest.display()));

    // Lstat, not stat: a symlink that resolves to a directory reads as a
    // directory through the usual metadata call, and redirecting git at some
    // other repository is exactly what this refuses.
    let dotgit = dest.join(".git");
    let md = std::fs::symlink_metadata(&dotgit)
        .map_err(|_| refuse(".git is missing at the worktree root"))?;
    if md.file_type().is_symlink() {
        return Err(refuse(".git is a symlink"));
    }
    if !md.is_dir() && !md.is_file() {
        return Err(refuse(".git is neither a directory nor a gitfile"));
    }

    // Git must agree about where its own directory lives: in the claim
    // repository's own linked-worktree storage. `dest` is always built by
    // `worktree::create` → `git worktree add`, which registers exactly one
    // place, `<common dir>/worktrees/<name>`, and a `git init` under `dest`
    // can never land there — so anything outside it is a repository the
    // harness chose (its own base, ancestry and history), whatever it merely
    // resembles: under `dest`, under `repo`, or under `repo/.git` itself.
    let git_dir = worker_git(dest, &["rev-parse", "--absolute-git-dir"])
        .map_err(|e| refuse(&format!("git refuses this worktree: {e}")))?;
    let resolved = canonical(Path::new(&git_dir));
    let common = worker_git(repo, &["rev-parse", "--git-common-dir"])
        .map_err(|e| refuse(&format!("claim repository has no git dir: {e}")))?;
    // A relative answer is relative to git's own cwd, which is `repo`.
    let common = if Path::new(&common).is_absolute() {
        PathBuf::from(&common)
    } else {
        repo.join(&common)
    };
    let common = canonical(&common);
    let storage = common.join("worktrees");
    if !resolved.starts_with(&storage) {
        return Err(refuse(&format!(
            "git directory {git_dir} is not in the claim repository's worktree storage {}",
            storage.display()
        )));
    }
    // …and it must see `dest` as the top of the worktree it is standing in.
    let top = worker_git(dest, &["rev-parse", "--show-toplevel"])
        .map_err(|e| refuse(&format!("git refuses this worktree: {e}")))?;
    if canonical(Path::new(&top)) != canonical(dest) {
        return Err(refuse(&format!("worktree top {top} is not the worktree")));
    }

    // The claimed base must exist (and not read as an option) and still be an
    // ancestor of HEAD.
    check_rev(base).map_err(|e| refuse(&format!("claim base {base:?}: {e}")))?;
    if worker_git(dest, &["merge-base", "--is-ancestor", base, "HEAD"]).is_err() {
        return Err(refuse(&format!(
            "HEAD does not descend from claim base {base}"
        )));
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    #[test]
    fn attempts_have_private_tmp_directories() {
        let first = super::attempt_tmp().unwrap();
        let second = super::attempt_tmp().unwrap();
        assert_ne!(first.path(), second.path());
        assert_ne!(first.path(), std::env::temp_dir());
        assert_ne!(second.path(), std::env::temp_dir());
        let path = first.path().to_path_buf();
        drop(first);
        assert!(!path.exists());
    }
    use super::*;
    use std::process::{Command, Stdio};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::time::Duration;
    use tempfile::TempDir;

    /// A harness that runs `sh -c <script>` in the worktree and counts how many
    /// times its command was built, so "never ran" is observable.
    struct Fake {
        script: &'static str,
        runs: AtomicUsize,
    }

    impl Fake {
        fn new(script: &'static str) -> Self {
            Fake {
                script,
                runs: AtomicUsize::new(0),
            }
        }

        fn runs(&self) -> usize {
            self.runs.load(Ordering::SeqCst)
        }
    }

    impl Harness for Fake {
        fn name(&self) -> &str {
            "fake"
        }

        fn command(&self, run: &HarnessRun) -> Command {
            self.runs.fetch_add(1, Ordering::SeqCst);
            let mut c = Command::new("sh");
            c.arg("-c").arg(self.script).current_dir(run.workdir);
            c
        }
    }

    // Recorded `opencode run --format json` streams; tests must not edit them.
    const RATE_LIMITED_429: &str =
        include_str!("../tests/fixtures/opencode/real-429-free-tier-quota.jsonl");
    const TRANSPORT_ERROR: &str =
        include_str!("../tests/fixtures/opencode/real-provider-transport-error.jsonl");
    const OK_TOOL_TOKENS: &str =
        include_str!("../tests/fixtures/opencode/real-ok-tool-tokens.jsonl");
    const OK_TEXT: &str = include_str!("../tests/fixtures/opencode/real-ok-text.jsonl");
    /// Recorded empty run: exit zero after announcing only its first step.
    const EMPTY_RUN: &str = include_str!("../tests/fixtures/opencode/real-empty-run.jsonl");

    /// A JSON-emitting harness that prints a recorded stream, optionally writes
    /// `out.txt`, and exits with `code` (the code opencode returned for it).
    struct JsonFixture {
        fixture: &'static str,
        code: u8,
        write_file: bool,
    }

    impl Harness for JsonFixture {
        fn name(&self) -> &str {
            "fake-json"
        }

        fn json_events(&self) -> bool {
            true
        }

        fn command(&self, run: &HarnessRun) -> Command {
            // The fixture is one argv element, never interpolated into the
            // script: its content is arbitrary JSON. It must be *printed*,
            // not opened: `"$1"` is the stream itself, not a path to it, so
            // `cat "$1"` would only report a failed lookup on stderr and the
            // classifier would see an empty run. The format string is a
            // constant, so backslashes in the JSON are never interpreted.
            let mut script = String::new();
            if self.write_file {
                script.push_str("printf 'hi\\n' > out.txt\n");
            }
            script.push_str(&format!("printf '%s' \"$1\"\nexit {}", self.code));
            let mut c = Command::new("sh");
            c.arg("-c")
                .arg(script)
                .arg("sh")
                .arg(self.fixture)
                .current_dir(run.workdir);
            c
        }
    }

    /// One attempt of a [`JsonFixture`] against a fresh repo and workroot.
    fn json_attempt(
        fixture: &'static str,
        code: u8,
        write_file: bool,
        check_cmd: &str,
    ) -> AttemptResult {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], check_cmd),
            1,
            root.path(),
        );
        let h = JsonFixture {
            fixture,
            code,
            write_file,
        };
        run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap()
    }

    /// Local git runner: worker-core's helper is `pub(crate)` there and its
    /// hermetic env only applies inside worker-core's own tests, so pin the
    /// identity here with `-c`.
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
        // Persist a local identity too: run_attempt's own `git` calls inherit
        // the machine's global config.
        git(p, &["config", "commit.gpgsign", "false"]);
        write(p, "README.md", "hello\n");
        let sha = commit_all(p, "init");
        (dir, sha)
    }

    /// Defaults with the watchdogs shrunk so tests finish quickly.
    fn tiny() -> Config {
        Config {
            stall: Duration::from_secs(5),
            start_to_close: Duration::from_secs(10),
            kill_grace: Duration::from_millis(200),
            check_tail_lines: 20,
            ..Config::default()
        }
    }

    fn claim(repo: &str, base: &str, allow: &[&str], check_cmd: &str) -> Claim {
        Claim {
            card: "c1".to_string(),
            repo: repo.to_string(),
            base_sha: base.to_string(),
            check_cmd: check_cmd.to_string(),
            // Most tests exercise attempt behavior, not the red-first gate.
            check_kind: "regression".to_string(),
            allow: allow.iter().map(|s| s.to_string()).collect(),
            forbid: Vec::new(),
            prompt: "do the thing".to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn base_check_scratch_is_absent_when_writer_runs() {
        let (r, _) = repo();
        write(r.path(), ".gitignore", "ignored\n");
        let base = commit_all(r.path(), "ignore check scratch");
        let root = tempfile::tempdir().unwrap();
        let mut c = claim(
            &r.path().to_string_lossy(),
            &base,
            &["out.txt"],
            "if test ! -f out.txt; then echo scratch > ignored; echo scratch > \"${TMPDIR:-.}/base-only\"; exit 1; fi",
        );
        c.check_kind = "change".into();
        let inp = input(c, 1, root.path());
        let h = Fake::new(
            "test ! -e ignored && test ! -e \"${TMPDIR:-.}/base-only\" && echo hi > out.txt",
        );
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(h.runs(), 1);
        assert_eq!(res.exit_class, ExitClass::Ok);
    }

    #[test]
    fn blank_change_check_does_not_park_writer() {
        for cmd in ["", " \t\n"] {
            let (r, base) = repo();
            let root = tempfile::tempdir().unwrap();
            let mut c = claim(&r.path().to_string_lossy(), &base, &["out.txt"], cmd);
            c.check_kind = "change".into();
            let inp = input(c, 1, root.path());
            let h = Fake::new("echo hi > out.txt");
            let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
            assert_eq!(h.runs(), 1);
            assert_eq!(res.exit_class, ExitClass::Ok);
            assert!(!res.check_tail.starts_with("red-first:"));
        }
    }

    #[test]
    fn red_first_check_runs_in_card_worktree() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let mut c = claim(
            &r.path().to_string_lossy(),
            &base,
            &["out.txt"],
            "test -f README.md || exit 0; if test ! -f out.txt; then echo scoped > marker; exit 1; fi",
        );
        c.check_kind = "change".into();
        let inp = input(c, 1, root.path());
        let h = Fake::new("test ! -e marker && echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        let dest = root
            .path()
            .join(AttemptId::new(inp.claim.card.clone(), inp.n).dir_name());
        assert!(!dest.join("marker").exists());
        assert_eq!(
            git(&dest, &["diff", "--name-only", &base, "HEAD"]),
            "out.txt"
        );
        assert!(!r.path().join("marker").exists());
        assert_eq!(h.runs(), 1);
        assert_eq!(res.exit_class, ExitClass::Ok);
    }

    #[test]
    fn red_first_untracked_file_outside_allow_is_not_writer_work() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let mut c = claim(
            &r.path().to_string_lossy(),
            &base,
            &["out.txt"],
            "if test ! -f out.txt; then echo scratch > outside; exit 1; fi",
        );
        c.check_kind = "change".into();
        let inp = input(c, 1, root.path());
        let h = Fake::new("test ! -e outside && echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::Ok);
        let dest = root
            .path()
            .join(AttemptId::new(inp.claim.card.clone(), inp.n).dir_name());
        assert!(!dest.join("outside").exists());
        assert_eq!(
            git(&dest, &["diff", "--name-only", &base, "HEAD"]),
            "out.txt"
        );
    }

    #[test]
    fn red_first_base_check_uses_fake_jail() {
        // Isolate env overrides in a child test process: other parallel tests
        // must never observe the fake jail or systemd launcher.
        if std::env::var_os("PG_FAKE_JAIL_TEST").is_some() {
            let (r, base) = repo();
            let root = tempfile::tempdir().unwrap();
            let mut c = claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true");
            c.check_kind = "change".into();
            let inp = input(c, 1, root.path());
            let h = Fake::new("exit 99");
            let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
            assert_eq!(res.exit_class, ExitClass::ContractDefect);
            assert!(res.check_tail.starts_with("red-first:"));
            assert_eq!(h.runs(), 0);
            return;
        }
        use std::os::unix::fs::PermissionsExt;
        let tools = tempfile::tempdir().unwrap();
        let marker = tools.path().join("invoked");
        let jail = tools.path().join("fake-jail");
        for (path, script) in [
            (
                tools.path().join("systemd-run"),
                "#!/bin/sh\nwhile [ \"$1\" != -- ]; do shift; done\nshift\nexec \"$@\"\n",
            ),
            (
                jail.clone(),
                "#!/bin/sh\nprintf invoked > \"$PG_FAKE_JAIL_TEST\"\nwhile [ \"$1\" != -- ]; do shift; done\nshift\nexec \"$@\"\n",
            ),
        ] {
            std::fs::write(&path, script).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let mut paths = vec![tools.path().to_path_buf()];
        paths.extend(std::env::split_paths(
            &std::env::var_os("PATH").unwrap_or_default(),
        ));
        let status = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "attempt::tests::red_first_base_check_uses_fake_jail",
            ])
            .env("PG_FAKE_JAIL_TEST", &marker)
            .env("PG_JAIL_BIN", &jail)
            .env("PATH", std::env::join_paths(paths).unwrap())
            .status()
            .unwrap();
        assert!(status.success());
        assert_eq!(std::fs::read_to_string(marker).unwrap(), "invoked");
    }

    #[test]
    // Real-jail base-check coverage runs only in the Rust CI job.
    fn red_first_check_is_jailed_in_card_worktree() {
        if std::env::var_os("PG_JAIL_BIN").is_none() {
            // rust-worker CI runs unjailed by design; the rust job exercises
            // the real Landlock jail in worker-core's check tests.
            eprintln!("skipping jailed base check: PG_JAIL_BIN is unset");
            return;
        }
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let outside = root.path().join("outside");
        let command = format!(
            "test -f README.md && echo scoped > marker && ! sh -c 'echo escaped > \"{}\"'",
            outside.display()
        );
        let mut c = claim(&r.path().to_string_lossy(), &base, &["out.txt"], &command);
        c.check_kind = "change".into();
        let inp = input(c, 1, root.path());
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert!(!outside.exists(), "base check escaped the attempt jail");
        let dest = root
            .path()
            .join(AttemptId::new(inp.claim.card.clone(), inp.n).dir_name());
        assert!(!dest.join("marker").exists());
        assert_eq!(res.exit_class, ExitClass::ContractDefect);
        assert!(res.check_tail.starts_with("red-first:"));
        assert_eq!(h.runs(), 0);
    }

    #[test]
    fn red_first_parks_passing_base_without_running_harness() {
        for kind in ["", "change"] {
            let (r, base) = repo();
            let root = tempfile::tempdir().unwrap();
            let mut c = claim(
                &r.path().to_string_lossy(),
                &base,
                &["out.txt"],
                "echo changed > README.md; echo scratch > marker; true",
            );
            c.check_kind = kind.into();
            let inp = input(c, 1, root.path());
            let h = Fake::new("echo hi > out.txt");
            let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
            assert_eq!(res.exit_class, ExitClass::ContractDefect);
            assert_eq!(h.runs(), 0);
            assert_eq!(res.head, base);
            let dest = root
                .path()
                .join(AttemptId::new(inp.claim.card.clone(), inp.n).dir_name());
            assert!(!dest.join("marker").exists());
            assert_eq!(
                std::fs::read_to_string(dest.join("README.md")).unwrap(),
                "hello\n"
            );
            assert_eq!(res.diff_hash, confine::diff_hash(r.path(), &base).unwrap());
            assert_eq!(
                res.check_tail,
                "red-first: check already passes on base sha (set check_kind=regression if intended)"
            );
        }
    }

    #[test]
    fn unknown_check_kind_is_a_contract_defect() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let mut c = claim(&r.path().to_string_lossy(), &base, &["out.txt"], "false");
        c.check_kind = "typo".into();
        let inp = input(c, 1, root.path());
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::ContractDefect);
        assert!(res.output_tail.contains("unknown check_kind"));
        assert_eq!(h.runs(), 0);
    }

    #[test]
    fn regression_skips_base_check() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let mut c = claim(
            &r.path().to_string_lossy(),
            &base,
            &["out.txt"],
            "test ! -f out.txt",
        );
        c.check_kind = "regression".into();
        let inp = input(c, 1, root.path());
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::CheckFail);
        assert_eq!(h.runs(), 1);
    }

    #[test]
    fn check_reads_are_protected_unless_allowed() {
        for allow in [
            vec!["out.txt"],
            vec!["out.txt", "*.md"],
            vec!["out.txt", "README.md"],
        ] {
            let (r, base) = repo();
            let root = tempfile::tempdir().unwrap();
            let mut c = claim(
                &r.path().to_string_lossy(),
                &base,
                &allow,
                "test -f out.txt",
            );
            c.check_reads = vec!["README.md".into()];
            let inp = input(c, 1, root.path());
            let h = Fake::new("echo changed > README.md; echo hi > out.txt");
            let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
            if allow.len() == 1 {
                assert_eq!(res.exit_class, ExitClass::ContractDefect);
                assert_eq!(res.check_tail, "forbidden: README.md");
            } else {
                assert_eq!(res.exit_class, ExitClass::Ok);
            }
            assert_eq!(h.runs(), 1);
        }
    }

    fn input(claim: Claim, n: u32, workroot: &Path) -> AttemptInput {
        AttemptInput {
            claim,
            n,
            leg: LegSpec {
                id: "leg-1".to_string(),
                tier: Tier::Free,
                model: "some/model".to_string(),
                harness: "opencode".to_string(),
            },
            workroot: workroot.to_string_lossy().into_owned(),
            class: String::new(),
            answers: Vec::new(),
        }
    }

    /// The `.git` rule is the only thing standing between a healthy worktree
    /// and a redirected one below: git happily follows the planted link, and
    /// every other check still passes — so a test that runs without the
    /// symlink rule fails here.
    #[test]
    fn verify_refuses_a_worktree_whose_dot_git_is_a_symlink() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        let dest = root.path().join(id.dir_name());
        worktree::create(r.path(), &base, &dest, &id).unwrap();
        // What git made passes as it is: the worktree's `.git` is a gitfile.
        super::verify_git_dir(&dest, r.path(), &base).unwrap();

        // Redirect `.git` at the claim repository's own git directory.
        std::fs::remove_file(dest.join(".git")).unwrap();
        std::os::unix::fs::symlink(r.path().join(".git"), dest.join(".git")).unwrap();
        // Git follows the link without complaint, and HEAD still descends
        // from the base: nothing but the symlink itself is refused.
        assert_eq!(
            super::worker_git(&dest, &["rev-parse", "--abbrev-ref", "HEAD"]).unwrap(),
            "main"
        );
        assert!(super::worker_git(&dest, &["merge-base", "--is-ancestor", &base, "HEAD"]).is_ok());
        let err = super::verify_git_dir(&dest, r.path(), &base).unwrap_err();
        assert!(
            matches!(&err, AttemptError::GitDir(m) if m.contains("symlink")),
            "{err:?}"
        );
    }

    /// A hook in the repository is code from the worktree, never from this
    /// plan: the worker's commit must not run it, while an ordinary commit
    /// still does — the control below proves the hook is planted and live.
    #[test]
    fn a_planted_pre_commit_hook_never_runs_as_the_worker() {
        use std::os::unix::fs::PermissionsExt;
        let (r, base) = repo();
        let hook = r.path().join(".git/hooks");
        std::fs::create_dir_all(&hook).unwrap();
        let hook = hook.join("pre-commit");
        std::fs::write(&hook, "#!/bin/sh\nprintf ran > HOOK_RAN\n").unwrap();
        std::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o755)).unwrap();

        // Control: the very same hook fires for a plain git commit.
        write(r.path(), "control.txt", "x");
        commit_all(r.path(), "control");
        assert!(
            r.path().join("HOOK_RAN").exists(),
            "the planted hook must be live for this test to mean anything"
        );
        std::fs::remove_file(r.path().join("HOOK_RAN")).unwrap();

        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::Ok);
        assert_ne!(res.head, base, "the worker did commit");
        let dest = root.path().join(AttemptId::new("c1", 1).dir_name());
        assert!(
            !dest.join("HOOK_RAN").exists(),
            "the worker's commit ran a hook from the repository"
        );
    }

    /// The credential is in the worker's own environment — that is the point
    /// of the scrub — and a git child still cannot see it.
    #[test]
    fn pg_keeper_token_is_invisible_to_a_worker_git_child() {
        let (r, _base) = repo();
        // SAFETY: `set_var` races with other threads reading the environment.
        // The canary below is read by no other test, and is removed again
        // before this test returns.
        unsafe { std::env::set_var("PG_KEEPER_TOKEN", "keeper-canary") };
        assert_eq!(std::env::var("PG_KEEPER_TOKEN").unwrap(), "keeper-canary");

        // An alias that prints the child's own environment: the leak is
        // visible if, and only if, the scrub lets the variable through.
        git(r.path(), &["config", "alias.envdump", "!env"]);
        let seen = super::worker_git(r.path(), &["envdump"]).expect("alias runs");
        unsafe { std::env::remove_var("PG_KEEPER_TOKEN") };
        assert!(
            seen.contains("PATH="),
            "control: the alias must run: {seen}"
        );
        assert!(!seen.contains("PG_KEEPER_TOKEN"), "{seen}");
        assert!(seen.lines().all(|l| !l.starts_with("PG_")), "{seen}");
    }

    /// Both halves of the hook kill-switch travel on every worker git call:
    /// `core.hooksPath=/dev/null` for everything, `--no-verify` for `commit`.
    #[test]
    fn worker_git_never_leaves_a_hook_a_way_in() {
        let args = |a: &[&str]| -> Vec<String> {
            super::git_command(Path::new("."), a)
                .get_args()
                .map(|a| a.to_string_lossy().into_owned())
                .collect()
        };
        let add = args(&["add", "-A"]);
        let at = add.iter().position(|a| a == "-c").expect("-c for hooks");
        assert_eq!(add[at + 1], "core.hooksPath=/dev/null", "{add:?}");
        let commit = args(&["commit", "-m", "x"]);
        let at = commit.iter().position(|a| a == "commit").unwrap();
        assert_eq!(commit[at + 1], "--no-verify", "{commit:?}");
        assert!(
            commit.iter().any(|a| a == "core.hooksPath=/dev/null"),
            "{commit:?}"
        );
    }

    /// End to end: a harness that replaces the worktree's `.git` with a link
    /// never gets a commit — the link is a confine violation wherever it
    /// points, even at a target inside the worktree.
    #[test]
    fn harness_that_swaps_dot_git_for_a_symlink_gets_no_commit() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        let h = Fake::new("mv .git .git.real && ln -s .git.real .git");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(
            res.exit_class,
            ExitClass::ContractDefect,
            "{}",
            res.output_tail
        );
        assert!(res.output_tail.contains(".git"), "{}", res.output_tail);
        assert_eq!(res.head, base, "a commit was made on the swapped worktree");
    }

    /// The same redirect as a repository git accepts: deleting the gitfile and
    /// running `git init` leaves a real `.git` directory under `dest`, which
    /// the old containment ("under `dest` or under `repo`") allowed — and the
    /// harness can fetch the claim's base through the gitfile it just read, so
    /// its own repository's `HEAD` even descends from the base. The attempt
    /// must refuse that repository before anything is staged, and must leave
    /// the claim's branch untouched.
    #[test]
    fn harness_that_replaces_the_gitfile_with_git_init_gets_no_commit() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        // Read the gitfile before deleting it, then build a repository of the
        // harness's own on top of the claim's base and commit in it: with a
        // `.git` directory under `dest`, every old check passes.
        let h = Fake::new(
            "src=$(sed 's/^gitdir: //' .git) && rm .git && git init -q \
             && git fetch -q \"$src\" HEAD && git reset --hard -q FETCH_HEAD \
             && echo pwned > out.txt && git add -A \
             && git -c user.name=harness -c user.email=harness@localhost \
             commit -q -m owned",
        );
        let dest = root.path().join(AttemptId::new("c1", 1).dir_name());
        let err = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap_err();
        assert!(
            matches!(&err, AttemptError::GitDir(m) if m.contains("worktree storage")),
            "{err:?}"
        );
        // Control: the recipe really ran and really produced a commit past the
        // base — the refusal is about where that repository lives, not about a
        // harness command that failed.
        assert!(dest.join(".git").is_dir(), "the harness's git init ran");
        let own = worker_git(&dest, &["rev-parse", "HEAD"]).unwrap();
        assert_ne!(own, base, "control: the harness committed in its own repo");
        // …and the attempt committed nothing: the claim's branch still sits at
        // the base, so no attacker-chosen head can be reported or merged.
        let branch = AttemptId::new("c1", 1).branch();
        assert_eq!(
            git(r.path(), &["rev-parse", &branch]),
            base,
            "the attempt branch moved"
        );
        assert_eq!(
            git(r.path(), &["rev-parse", "HEAD"]),
            base,
            "the claim repository gained a commit"
        );
    }

    #[test]
    fn clean_harness_and_passing_check_is_ok() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::Ok);
        assert_eq!(res.branch, AttemptId::new("c1", 1).branch());
        assert_ne!(res.head, base);
        assert!(!res.diff_hash.is_empty());
        assert_eq!(h.runs(), 1);
    }

    #[test]
    fn a_rate_limit_is_usage_limit_whatever_the_exit_code() {
        // The provider error is on the event stream, so it outranks a clean
        // exit, and the check never runs: `false` would otherwise fail it.
        for code in [1u8, 0] {
            let res = json_attempt(RATE_LIMITED_429, code, false, "false");
            assert_eq!(res.exit_class, ExitClass::UsageLimit, "exit {code}");
            assert_eq!(res.check_hash, "", "exit {code}");
        }
    }

    #[test]
    fn a_transport_error_is_an_outage_whatever_the_exit_code() {
        for code in [1u8, 0] {
            let res = json_attempt(TRANSPORT_ERROR, code, false, "true");
            assert_eq!(res.exit_class, ExitClass::Outage, "exit {code}");
        }
    }

    #[test]
    fn a_real_empty_run_is_classified_outage_never_ok() {
        let res = json_attempt(EMPTY_RUN, 0, false, "true");
        assert_eq!(res.exit_class, ExitClass::Outage);
        assert_eq!(res.check_hash, "");
    }

    #[test]
    fn a_tool_run_reports_tokens_and_is_ok_once_the_check_passes() {
        let res = json_attempt(OK_TOOL_TOKENS, 0, true, "test -f out.txt");
        assert_eq!(res.exit_class, ExitClass::Ok);
        assert_eq!(res.tokens_in, Some(13318));
        assert_eq!(res.tokens_out, Some(50));
        assert!(!res.check_hash.is_empty());
    }

    #[test]
    fn a_text_only_run_with_no_diff_is_no_change() {
        let res = json_attempt(OK_TEXT, 0, false, "true");
        assert_eq!(res.exit_class, ExitClass::NoChange);
        assert_eq!(res.check_hash, "");
    }

    #[test]
    fn a_plain_harness_with_no_diff_is_also_no_change() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(claim(&repo_s, &base, &["out.txt"], "true"), 1, root.path());
        let h = Fake::new("true");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::NoChange);
        assert_eq!(res.check_hash, "");
        assert_eq!(h.runs(), 1);
    }

    #[test]
    fn work_the_agent_committed_itself_is_not_no_change() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        // The harness commits its own work, so the index matches HEAD with
        // nothing staged: only HEAD having left `base` shows there is work.
        let h = Fake::new(
            "printf 'hi\\n' > out.txt && git add out.txt && \
             git -c user.name=agent -c user.email=agent@localhost \
             commit -q -m 'agent work'",
        );
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_ne!(res.exit_class, ExitClass::NoChange);
        assert_eq!(res.exit_class, ExitClass::Ok);
        assert!(!res.check_hash.is_empty());
        assert_eq!(h.runs(), 1);
    }

    #[test]
    fn write_outside_the_allow_set_is_a_contract_defect() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["other.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::ContractDefect);
    }

    #[test]
    fn failing_harness_is_not_ok() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        let h = Fake::new("exit 3");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::WorkerCrash);
        assert_ne!(res.exit_class, ExitClass::Ok);
    }

    #[test]
    fn failing_check_is_check_fail() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "exit 1"),
            1,
            root.path(),
        );
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::CheckFail);
    }

    #[test]
    fn check_that_fails_then_passes_is_flaky() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let mut inp = input(
            claim(
                &repo_s,
                &base,
                &["out.txt"],
                "test -f m || { touch m; exit 1; }",
            ),
            1,
            root.path(),
        );
        inp.claim.check_kind = "regression".into();
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::FlakyCheck);
    }

    #[test]
    fn cancelled_before_the_harness_runs() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| true).unwrap();
        assert_eq!(res.exit_class, ExitClass::Cancelled);
        assert_eq!(h.runs(), 0, "harness must not run when cancelled");
    }

    #[test]
    fn cancel_while_the_harness_runs_stops_it_without_committing() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        // A long silent harness: without a cancel poll it would stall out.
        let h = Fake::new("sleep 600");
        // The closure itself never blocks; a separate thread flips the flag.
        let flag = Arc::new(AtomicBool::new(false));
        let setter = Arc::clone(&flag);
        let ticker = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(500));
            setter.store(true, Ordering::SeqCst);
        });
        let cancelled = || flag.load(Ordering::SeqCst);
        let start = std::time::Instant::now();
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &cancelled).unwrap();
        ticker.join().unwrap();
        assert_eq!(res.exit_class, ExitClass::Cancelled);
        assert!(
            start.elapsed() < Duration::from_secs(10),
            "cancel must not wait for the harness to exit"
        );
        assert_eq!(res.head, base, "a cancelled attempt must not commit");
    }

    #[test]
    fn each_attempt_gets_its_own_worktree() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let h = Fake::new("echo hi > out.txt");
        let cfg = tiny();
        for n in [1u32, 2] {
            let inp = input(
                claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
                n,
                root.path(),
            );
            let res = run_attempt(&h, &cfg, &Patterns::default(), &inp, &|| false).unwrap();
            assert_eq!(res.exit_class, ExitClass::Ok);
        }
        let d1 = root.path().join(AttemptId::new("c1", 1).dir_name());
        let d2 = root.path().join(AttemptId::new("c1", 2).dir_name());
        assert!(d1.exists() && d2.exists());
        assert_ne!(d1, d2);
    }

    #[test]
    fn budget_mapping_carries_leg_and_tier() {
        let r = AttemptResult {
            exit_class: ExitClass::CheckFail,
            diff_hash: "d".to_string(),
            check_hash: "c".to_string(),
            check_tail: "boom".to_string(),
            context_overflow: true,
            branch: "attempt/c1/1".to_string(),
            head: "h".to_string(),
            output_tail: "o".to_string(),
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
        };
        let leg = LegSpec {
            id: "leg-9".to_string(),
            tier: Tier::Paid,
            model: "m".to_string(),
            harness: "opencode".to_string(),
        };
        let b = to_budget_attempt(&r, &leg);
        assert_eq!(b.exit_class, ExitClass::CheckFail);
        assert_eq!(b.leg, "leg-9");
        assert_eq!(b.tier, Tier::Paid);
        assert_eq!(b.diff_hash, "d");
        assert_eq!(b.check_hash, "c");
        assert!(b.context_overflow);
    }

    fn at(secs: u64, ms: u64) -> std::time::SystemTime {
        std::time::UNIX_EPOCH + Duration::from_secs(secs) + Duration::from_millis(ms)
    }

    #[test]
    fn iso_utc_formats_rfc3339_millis() {
        assert_eq!(iso_utc(at(0, 0)), "1970-01-01T00:00:00.000Z");
        assert_eq!(iso_utc(at(1_000_000_000, 0)), "2001-09-09T01:46:40.000Z");
        assert_eq!(iso_utc(at(1_500, 0)), "1970-01-01T00:25:00.000Z");
        assert_eq!(iso_utc(at(0, 1_500)), "1970-01-01T00:00:01.500Z");
    }

    #[test]
    fn iso_utc_handles_leap_days() {
        assert_eq!(iso_utc(at(1_709_164_800, 0)), "2024-02-29T00:00:00.000Z");
        assert_eq!(iso_utc(at(951_782_400, 0)), "2000-02-29T00:00:00.000Z");
    }

    #[test]
    fn iso_utc_clamps_pre_epoch_to_epoch() {
        let before = std::time::UNIX_EPOCH - Duration::from_secs(60);
        assert_eq!(iso_utc(before), "1970-01-01T00:00:00.000Z");
    }

    #[test]
    fn result_omits_none_metadata_and_reads_old_json() {
        let r = AttemptResult {
            exit_class: ExitClass::Ok,
            diff_hash: "d".to_string(),
            check_hash: "c".to_string(),
            check_tail: "t".to_string(),
            context_overflow: false,
            branch: "b".to_string(),
            head: "h".to_string(),
            output_tail: "o".to_string(),
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
        };
        let v = serde_json::to_value(&r).unwrap();
        let obj = v.as_object().unwrap();
        assert_eq!(obj.len(), 8, "only the original eight keys serialize");
        assert!(!obj.contains_key("harness"));
        assert!(!obj.contains_key("log_ref"));

        // A result written before the projector keys existed still loads.
        let old = r#"{"exit_class":"ok","diff_hash":"d","check_hash":"c","check_tail":"t","context_overflow":false,"branch":"b","head":"h","output_tail":"o"}"#;
        let back: AttemptResult = serde_json::from_str(old).unwrap();
        assert_eq!(back.exit_class, ExitClass::Ok);
        assert_eq!(back.harness, None);
        assert_eq!(back.tokens_in, None);
    }

    #[test]
    fn leg_without_harness_reads_as_opencode() {
        let leg: LegSpec = serde_json::from_str(r#"{"id":"l","tier":"free","model":"m"}"#).unwrap();
        assert_eq!(leg.harness, "opencode");
        let leg: LegSpec =
            serde_json::from_str(r#"{"id":"l","tier":"free","model":"m","harness":"claude"}"#)
                .unwrap();
        assert_eq!(leg.harness, "claude");
    }

    #[test]
    fn question_line_ends_needs_input_with_the_parsed_question() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(claim(&repo_s, &base, &["out.txt"], "false"), 1, root.path());
        let h = Fake::new(
            r#"echo 'PG_QUESTION: {"text":"which db?","default":"sqlite","deadline_s":7,"risk_tags":[]}'"#,
        );
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::NeedsInput);
        let q = res.question.expect("the question rides on the result");
        assert_eq!(q.text, "which db?");
        assert_eq!(q.default.as_deref(), Some("sqlite"));
        assert_eq!(q.deadline_s, Some(7));
    }

    #[test]
    fn malformed_question_is_a_contract_defect() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let inp = input(claim(&repo_s, &base, &["out.txt"], "false"), 1, root.path());
        let h = Fake::new("echo 'PG_QUESTION: not json'");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::ContractDefect);
        assert!(res.question.is_none());
        assert!(res.check_tail.contains("PG_QUESTION"), "{}", res.check_tail);
    }

    #[test]
    fn answers_reach_the_harness_prompt() {
        struct Echo;
        impl Harness for Echo {
            fn name(&self) -> &str {
                "echo"
            }
            fn command(&self, run: &HarnessRun) -> Command {
                let mut c = Command::new("sh");
                c.arg("-c")
                    .arg("printf %s \"$0\" > out.txt")
                    .arg(run.prompt)
                    .current_dir(run.workdir);
                c
            }
        }
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let mut inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            2,
            root.path(),
        );
        inp.answers = vec!["use sqlite".to_string()];
        let res = run_attempt(&Echo, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::Ok);
        let written = std::fs::read_to_string(
            root.path()
                .join(AttemptId::new("c1", 2).dir_name())
                .join("out.txt"),
        )
        .unwrap();
        assert!(written.starts_with("do the thing"), "{written}");
        assert!(written.ends_with("use sqlite"), "{written}");
    }

    #[test]
    fn unknown_harness_is_a_contract_defect_with_a_worktree() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let mut inp = input(claim(&repo_s, &base, &["x"], "true"), 3, root.path());
        inp.leg.harness = "no-such".to_string();
        let res = run_attempt_with(None, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.exit_class, ExitClass::ContractDefect);
        assert!(res.output_tail.contains("no-such"), "{}", res.output_tail);
        assert_eq!(res.harness.as_deref(), Some("no-such"));
        assert_eq!(res.combo.as_deref(), Some("leg-1"));
        // The worktree exists so the probe that follows has something to look at.
        assert!(
            root.path()
                .join(AttemptId::new("c1", 3).dir_name())
                .is_dir()
        );
    }

    #[test]
    fn run_attempt_stamps_provider_metadata() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let repo_s = r.path().to_string_lossy().into_owned();
        let mut inp = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            1,
            root.path(),
        );
        inp.class = "128k".to_string();
        let h = Fake::new("echo hi > out.txt");
        let res = run_attempt(&h, &tiny(), &Patterns::default(), &inp, &|| false).unwrap();
        assert_eq!(res.harness.as_deref(), Some("fake"));
        assert_eq!(res.model.as_deref(), Some("some/model"));
        assert_eq!(res.combo.as_deref(), Some("leg-1"));
        assert_eq!(res.class.as_deref(), Some("128k"));
        assert!(res.started_at.is_some());
        assert!(res.ended_at.is_some());
        assert_eq!(res.tokens_in, None);

        // An empty class is unknown, not an empty string.
        let mut unknown = input(
            claim(&repo_s, &base, &["out.txt"], "test -f out.txt"),
            2,
            root.path(),
        );
        unknown.class = String::new();
        let res2 = run_attempt(&h, &tiny(), &Patterns::default(), &unknown, &|| false).unwrap();
        assert_eq!(res2.class, None);
    }
}
