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
use pg_worker_core::check::{self, CheckResult};
use pg_worker_core::confine::{self, ConfineError, Violation};
use pg_worker_core::config::{Config, Tier};
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
}

/// Everything one attempt needs to run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttemptInput {
    pub claim: Claim,
    pub n: u32,
    pub leg: LegSpec,
    /// Directory holding the attempt worktrees.
    pub workroot: String,
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
/// `cancelled` is polled once, before any harness work: a cancelled attempt
/// still gets its fresh worktree so the caller's bookkeeping matches reality.
pub fn run_attempt(
    h: &dyn Harness,
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
        return Ok(AttemptResult {
            exit_class: ExitClass::Cancelled,
            diff_hash: String::new(),
            check_hash: String::new(),
            check_tail: String::new(),
            context_overflow: false,
            branch,
            head: wt.head.clone(),
            output_tail: String::new(),
        });
    }

    // A link planted by the base that escapes the worktree is a contract
    // defect: a write through it lands beyond the write set.
    if let Some(paths) = sandbox_escape(&dest)? {
        return defect_result(&dest, &claim.base_sha, &branch, &wt.head, escape_tail(&paths));
    }

    let run = HarnessRun {
        workdir: dest.as_path(),
        model: inp.leg.model.as_str(),
        prompt: claim.prompt.as_str(),
    };
    let opts = Opts {
        stall: Some(cfg.stall),
        timeout: Some(cfg.start_to_close),
        grace: cfg.kill_grace,
        max_output: 1 << 20,
    };
    let sup = run_harness(h, &run, &opts)?;
    let output = String::from_utf8_lossy(&sup.output).into_owned();
    let output_tail = check::tail_lines(&output, cfg.check_tail_lines);
    let clean = sup.code == Some(0) && !sup.stalled && !sup.timed_out;

    // The harness itself must not have left an escaping link behind.
    if let Some(paths) = sandbox_escape(&dest)? {
        return defect_result(&dest, &claim.base_sha, &branch, &wt.head, escape_tail(&paths));
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
    let contract = !violations.is_empty();

    // Hashed before the check so a check's own scratch files never pollute the
    // diff of what the attempt changed.
    let diff_hash = confine::diff_hash(&dest, &claim.base_sha)?;
    let (outcome, check_hash, check_tail) = if clean && !contract {
        let (o, last) = check_with_rerun(&claim.check_cmd, &dest, cfg)?;
        (Some(o), last.output_hash, last.tail)
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
        stderr: output,
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
    use std::sync::atomic::{AtomicUsize, Ordering};
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
            },
            workroot: workroot.to_string_lossy().into_owned(),
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
        let inp = input(claim(&repo_s, &base, &["out.txt"], "exit 1"), 1, root.path());
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
            claim(&repo_s, &base, &["out.txt"], "test -f m || { touch m; exit 1; }"),
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
        };
        let leg = LegSpec {
            id: "leg-9".to_string(),
            tier: Tier::Paid,
            model: "m".to_string(),
        };
        let b = to_budget_attempt(&r, &leg);
        assert_eq!(b.exit_class, ExitClass::CheckFail);
        assert_eq!(b.leg, "leg-9");
        assert_eq!(b.tier, Tier::Paid);
        assert_eq!(b.diff_hash, "d");
        assert_eq!(b.check_hash, "c");
        assert!(b.context_overflow);
    }
}
