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
use crate::question::{self, Question, Scan};
use pg_worker_core::check::{self, CheckResult};
use pg_worker_core::config::{Config, Tier};
use pg_worker_core::confine::{self, ConfineError, Violation};
use pg_worker_core::exit_class::{CheckOutcome, ExitClass, Patterns, Signals};
use pg_worker_core::git::{GitError, git};
use pg_worker_core::stall::Opts;
use pg_worker_core::worktree::{self, AttemptId};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

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
    let dest = Path::new(&inp.workroot).join(format!("{}-{}", claim.card, inp.n));

    // A retry or a promotion always starts from a fresh tree; an old one is
    // never reused.
    clear_dest(repo, &dest)?;
    let wt = worktree::create(repo, &claim.base_sha, &dest, &id)?;

    if cancelled() {
        return Ok(cancelled_result(&branch, &wt.head, String::new()));
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
    let opts = Opts {
        stall: Some(cfg.stall),
        timeout: Some(cfg.start_to_close),
        grace: cfg.kill_grace,
        max_output: 1 << 20,
    };
    // The agent's private HOME lives beside the worktree (never in its diff) and
    // goes away with the attempt; the check below reuses it.
    let home = pg_worker_core::sandbox_env::HomeGuard::for_dir(&dest)?;
    let sup = run_harness(h, &run, home.path(), &opts, cancelled)?;
    let output = String::from_utf8_lossy(&sup.output).into_owned();
    let output_tail = check::tail_lines(&output, cfg.check_tail_lines);

    // A cancel that arrived while the harness ran kills the agent, so nothing
    // it wrote is committed and the commit/confinement/check stages are
    // skipped: the caller only needs to know the attempt was cancelled.
    if sup.cancelled {
        return Ok(cancelled_result(&branch, &wt.head, output_tail));
    }

    let clean = sup.code == Some(0) && !sup.stalled && !sup.timed_out;

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

    if clean {
        git(&dest, &["add", "-A"])?;
        // `--quiet` implies `--exit-code`: a non-zero exit means something is
        // staged. A clean tree must not create an empty commit.
        if git(&dest, &["diff", "--cached", "--quiet"]).is_err() {
            git(
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
    }
    let head = git(&dest, &["rev-parse", "HEAD"])?;

    // Confinement is judged before the check: a write outside the set is a
    // contract defect however the check would have gone, so the check is
    // skipped entirely.
    let violations =
        confine::check_confinement(&dest, &claim.base_sha, &claim.allow, &claim.forbid)?;
    let contract = !violations.is_empty() || malformed.is_some();

    // Hashed before the check so a check's own scratch files never pollute the
    // diff of what the attempt changed.
    let diff_hash = confine::diff_hash(&dest, &claim.base_sha)?;
    let (outcome, check_hash, check_tail) = if clean && !contract && question.is_none() {
        let (o, last) = check_with_rerun(&claim.check_cmd, &dest, cfg)?;
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
        tokens_in: None,
        tokens_out: None,
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
) -> Result<(CheckOutcome, CheckResult), AttemptError> {
    let first = check::run_check(cmd, dest, cfg.start_to_close, cfg.check_tail_lines)?;
    if first.passed() {
        return Ok((CheckOutcome::Pass, first));
    }
    let second = check::run_check(cmd, dest, cfg.start_to_close, cfg.check_tail_lines)?;
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

#[cfg(all(test, unix))]
mod tests {
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
            allow: allow.iter().map(|s| s.to_string()).collect(),
            forbid: Vec::new(),
            prompt: "do the thing".to_string(),
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
        assert_eq!(res.branch, "attempt/c1/1");
        assert_ne!(res.head, base);
        assert!(!res.diff_hash.is_empty());
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
        let inp = input(
            claim(
                &repo_s,
                &base,
                &["out.txt"],
                "test -f m || { touch m; exit 1; }",
            ),
            1,
            root.path(),
        );
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
        let d1 = root.path().join("c1-1");
        let d2 = root.path().join("c1-2");
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
        let inp = input(claim(&repo_s, &base, &["out.txt"], "true"), 1, root.path());
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
        let written = std::fs::read_to_string(root.path().join("c1-2/out.txt")).unwrap();
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
        assert!(root.path().join("c1-3").is_dir());
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
