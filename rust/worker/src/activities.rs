//! Temporal activities and the wire types they exchange with the card workflow.
//!
//! Every activity here is a thin, side-effecting shell: the deterministic parts
//! live in `pg_worker_core` (probe, janitor, budget) and the blocking parts in
//! the sibling modules (attempt, mergelock, keeper). The activity body owns the
//! engine-specific concerns the workflow must not have: wall clock, jitter,
//! heartbeats and cancellation. The mapping helpers are factored out as plain
//! `pub(crate)` functions so they can be tested without a Temporal server.

use crate::attempt::{self, AttemptInput, AttemptResult, LegSpec};
use crate::harness::Harness;
use crate::keeper::{Alarm, AttemptReport, Claim, Keeper};
use crate::mergelock::{self, MergeOutcome};
use pg_worker_core::budget;
use pg_worker_core::config::Config;
use pg_worker_core::exit_class::Patterns;
use pg_worker_core::git::{GitError, git};
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

/// Input of the `card` workflow: the claim, the legs it may run on, and where.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CardInput {
    pub claim: Claim,
    pub legs: Vec<LegSpec>,
    pub workroot: String,
    pub main_branch: String,
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

/// Fast-forward an attempt branch into main, guarded by the merge lock.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MergeInput {
    pub repo: String,
    pub main_branch: String,
    pub branch: String,
    pub base_sha: String,
}

/// Outcome of a merge: `merged` | `base_moved` | `conflict`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MergeResult {
    pub outcome: String,
    pub sha: Option<String>,
    pub main_head: String,
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
    pub harness: Arc<dyn Harness>,
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
        },
        MergeOutcome::BaseMoved => MergeResult {
            outcome: "base_moved".to_string(),
            sha: None,
            main_head,
        },
        MergeOutcome::Conflict => MergeResult {
            outcome: "conflict".to_string(),
            sha: None,
            main_head,
        },
    }
}

/// Probe the worktree of `card#n` in `workroot`. A missing worktree or a bad
/// base is reported as `error`, never as `nothing`.
pub(crate) fn do_probe(cfg: &Config, input: &ProbeInput) -> ProbeResult {
    let id = AttemptId::new(input.card.clone(), input.n);
    let dir = Path::new(&input.workroot).join(format!("{}-{}", input.card, input.n));
    let outcome = probe::probe(
        &dir,
        &input.base_sha,
        &input.check_cmd,
        cfg.start_to_close,
        cfg.check_tail_lines,
    );
    map_probe(outcome, id.branch())
}

/// Merge `branch` into `main_branch` under the lock, then read main back.
pub(crate) fn do_merge(input: &MergeInput) -> Result<MergeResult, mergelock::MergeError> {
    let repo = Path::new(&input.repo);
    let outcome = mergelock::merge_attempt(
        repo,
        &input.main_branch,
        &input.branch,
        &input.base_sha,
        MERGE_LOCK_TIMEOUT,
    )?;
    let main_head = git(repo, &["rev-parse", &input.main_branch])?;
    Ok(map_merge(outcome, main_head))
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
        let mut handle = {
            let cancel_flag = cancel_flag.clone();
            let input = input.clone();
            tokio::task::spawn_blocking(move || {
                let is_cancelled = || cancel_flag.load(Ordering::SeqCst);
                attempt::run_attempt(
                    &*deps.harness,
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
        tokio::task::spawn_blocking(move || do_merge(&input))
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
    use crate::harness::HarnessRun;
    use crate::keeper::StubKeeper;
    use pg_worker_core::config::Tier;
    use pg_worker_core::worktree;
    use std::process::{Command, Stdio};
    use tempfile::TempDir;

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
            harness: Arc::new(Fake),
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
            }],
            workroot: "/w".to_string(),
            main_branch: "main".to_string(),
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
        });
        round_trip(&MergeResult {
            outcome: "merged".to_string(),
            sha: Some("sha".to_string()),
            main_head: "sha".to_string(),
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
        let wt = worktree::create(r.path(), &base, &root.path().join("c1-1"), &id).unwrap();
        write(&wt.path, "done.txt", "x");
        commit_all(&wt.path, "work");

        let input = probe_input(r.path(), root.path(), 1, &base, "test -f done.txt");
        let res = do_probe(&tiny(), &input);
        assert_eq!(res.outcome, "already_done");
        assert_eq!(res.branch, "attempt/c1/1");

        // A worktree with nothing beyond base is `nothing`, even if dirty.
        let id2 = AttemptId::new("c1", 2);
        let wt2 = worktree::create(r.path(), &base, &root.path().join("c1-2"), &id2).unwrap();
        write(&wt2.path, "wip.txt", "x");
        let input2 = probe_input(r.path(), root.path(), 2, &base, "true");
        assert_eq!(do_probe(&tiny(), &input2).outcome, "nothing");
    }

    #[test]
    fn do_probe_reports_error_for_a_bad_base() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        worktree::create(r.path(), &base, &root.path().join("c1-1"), &id).unwrap();

        let input = probe_input(r.path(), root.path(), 1, "deadbeef", "true");
        assert_eq!(do_probe(&tiny(), &input).outcome, "error");
    }

    #[test]
    fn do_merge_fast_forwards_main() {
        let (r, base) = repo();
        let p = r.path();
        let head = attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        let input = MergeInput {
            repo: p.to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
            branch: "attempt/c1/1".to_string(),
            base_sha: base,
        };
        let res = do_merge(&input).unwrap();
        assert_eq!(res.outcome, "merged");
        assert_eq!(res.sha, Some(head.clone()));
        assert_eq!(res.main_head, head);
    }

    #[test]
    fn do_merge_reports_a_moved_base() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");
        let input = MergeInput {
            repo: p.to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
            branch: "attempt/c1/1".to_string(),
            base_sha: base,
        };
        let res = do_merge(&input).unwrap();
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
        let input = MergeInput {
            repo: p.to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
            branch: "attempt/orphan/1".to_string(),
            base_sha: base.clone(),
        };
        let res = do_merge(&input).unwrap();
        assert_eq!(res.outcome, "conflict");
        assert_eq!(res.sha, None);
        assert_eq!(res.main_head, base);
    }

    #[test]
    fn do_janitor_removes_dead_attempts() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        let wt = worktree::create(r.path(), &base, &root.path().join("c1-1"), &id).unwrap();
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
