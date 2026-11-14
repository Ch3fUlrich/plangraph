#![allow(dead_code, unused_imports)]
use pg_worker::activities::{CardActivities, CardOutput, Deps, TASK_QUEUE};
use pg_worker::attempt::LegSpec;
use pg_worker::harness::{Harness, HarnessRegistry, HarnessRun};
use pg_worker::keeper::{Claim, Keeper, KeeperError, StubKeeper};
use pg_worker::phase::{PhaseInput, PhaseOutput};
use pg_worker::phase_workflow::Phase;
use pg_worker::workflow::{Card, CardInput};
use pg_worker_core::config::{Config, Tier};
use pg_worker_core::exit_class::ExitClass;
use pg_worker_core::exit_class::Patterns;
use pg_worker_core::redact::REDACTED;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use tempfile::TempDir;
use temporalio_client::{
    Client, ClientOptions, Connection, ConnectionOptions, Url, WorkflowFetchHistoryOptions,
    WorkflowGetResultOptions, WorkflowHandle, WorkflowSignalOptions, WorkflowStartOptions,
};
use temporalio_sdk::{Runtime, Worker, WorkerOptions};
pub(super) type Handle = WorkflowHandle<Client, Card>;
/// Runs `sh -c <script>` inside the attempt's worktree, standing in for a real
/// agent CLI so a test can dictate exactly what the attempt does. The leg's
/// model reaches the script as `$PG_MODEL`, so one script can behave
/// differently per leg (for example, hit a limit on one model only).
pub(super) struct ShellHarness {
    script: String,
}
impl Harness for ShellHarness {
    fn name(&self) -> &str {
        "shell"
    }

    fn command(&self, run: &HarnessRun) -> Command {
        let mut cmd = Command::new("sh");
        cmd.arg("-c")
            .arg(&self.script)
            .env("PG_MODEL", run.model)
            .env("PG_PROMPT", run.prompt)
            .current_dir(run.workdir);
        cmd
    }
}

/// One self-contained fixture: a real git repo, a workroot, a stub keeper, a
/// shell harness and a worker polling a private task queue.
pub(super) struct Env {
    /// Kept only so the temporary directories outlive the worker.
    _repo: TempDir,
    pub(super) repo: PathBuf,
    pub(super) base_sha: String,
    _workroot: TempDir,
    pub(super) workroot: PathBuf,
    pub(super) keeper: Arc<StubKeeper>,
    pub(super) client: Client,
    /// The activities' dependencies, kept so a restarted worker can be built
    /// with the same keeper, harnesses and config.
    deps: Deps,
    /// The `PG_TEMPORAL_ADDR` url, kept so a restarted worker can reconnect.
    addr: String,
    pub(super) task_queue: String,
    worker: std::cell::RefCell<Option<tokio::task::JoinHandle<()>>>,
    /// A built worker not yet polling, for tests that signal before any worker runs.
    held: std::cell::RefCell<Option<Worker>>,
    /// Question wait cap handed to the next card, in seconds; unset = 24 h.
    question_cap_s: std::cell::Cell<Option<u64>>,
    max_rounds: std::cell::Cell<Option<u32>>,
    _runtime: Runtime,
}

impl Env {
    pub(super) async fn new(addr: &str, name: &str, script: &str, start: bool) -> Env {
        let repo = tempfile::tempdir().expect("repo tempdir");
        let repo_path = repo.path().to_path_buf();
        let base_sha = init_repo(&repo_path);
        let workroot = tempfile::tempdir().expect("workroot tempdir");
        let workroot_path = workroot.path().to_path_buf();

        let keeper = Arc::new(StubKeeper::default());
        // The shell stands in for every harness name a leg may use, so a test
        // picks the "family" of a leg purely by its `harness` field.
        let shell: Arc<dyn Harness> = Arc::new(ShellHarness {
            script: script.to_string(),
        });
        let harnesses = HarnessRegistry::new()
            .with("opencode", shell.clone())
            .with("claude", shell);
        // These timeouts are the activities' (heartbeat cadence, stall kill).
        // The long timeouts keep a starved host from ending an attempt by
        // timeout, so a cancel test can only pass via the cancel path; the
        // harness prints every second so the 30 s stall watchdog does not fire
        // either. The workflow keeps its own `Config::default()` for the retry
        // policy, so tests must not lean on workflow-side timeouts to finish.
        let cfg = Config {
            heartbeat: Duration::from_millis(200),
            heartbeat_timeout: Duration::from_secs(30),
            start_to_close: Duration::from_secs(300),
            stall: Duration::from_secs(30),
            ..Config::allow_all_for_tests()
        };
        // `Deps` wants a trait object; name the coercion explicitly so the
        // concrete `Arc<StubKeeper>` widens to `Arc<dyn Keeper>` here.
        let dyn_keeper: Arc<dyn Keeper> = keeper.clone();
        let deps = Deps {
            keeper: dyn_keeper,
            harnesses,
            cfg,
            patterns: Patterns::default(),
        };

        let runtime = Runtime::from_current_tokio(Default::default()).expect("temporal runtime");
        let url = Url::from_str(addr).expect("PG_TEMPORAL_ADDR parses as a url");
        let conn = Connection::connect(ConnectionOptions::new(url.clone()).build())
            .await
            .expect("connect test client");
        let client = Client::new(conn, ClientOptions::new("default").build()).expect("test client");

        // A unique task queue per test keeps parallel tests from stealing each
        // other's workflow and activity tasks.
        let task_queue = format!("{TASK_QUEUE}-{name}-{}", unique());
        let opts = WorkerOptions::new(task_queue.as_str())
            .register_workflow::<Card>()
            .expect("register card workflow")
            .register_workflow::<Phase>()
            .expect("register phase workflow")
            .register_activities(CardActivities::new(deps.clone()))
            .build();

        // The worker holds its own connection so the test client stays free to
        // start and signal workflows.
        let wconn = Connection::connect(ConnectionOptions::new(url).build())
            .await
            .expect("connect worker client");
        let wclient =
            Client::new(wconn, ClientOptions::new("default").build()).expect("worker client");
        let mut worker = Worker::new(&runtime, wclient, opts).expect("build worker");
        // The run loop only ends on shutdown or an unresolvable error; the test
        // aborts it in `Drop`, so the value is intentionally discarded.
        let (worker, held) = if start {
            let handle = tokio::task::spawn_local(async move {
                let _ = worker.run().await;
            });
            (Some(handle), None)
        } else {
            (None, Some(worker))
        };

        Env {
            _repo: repo,
            repo: repo_path,
            base_sha,
            _workroot: workroot,
            workroot: workroot_path,
            keeper,
            client,
            deps,
            addr: addr.to_string(),
            task_queue,
            worker: std::cell::RefCell::new(worker),
            held: std::cell::RefCell::new(held),
            question_cap_s: std::cell::Cell::new(None),
            max_rounds: std::cell::Cell::new(None),
            _runtime: runtime,
        }
    }

    /// A claim against this fixture's repo, with the write set and check given
    /// by the caller.
    pub(super) fn claim(&self, card: &str, allow: &[&str], check_cmd: &str) -> Claim {
        Claim {
            card: card.to_string(),
            repo: self.repo.to_string_lossy().into_owned(),
            base_sha: self.base_sha.clone(),
            check_cmd: check_cmd.to_string(),
            allow: allow.iter().map(|s| s.to_string()).collect(),
            forbid: Vec::new(),
            prompt: "do the thing".to_string(),
            ..Default::default()
        }
    }

    /// Start the card workflow for `id` and hand back a typed handle.
    ///
    /// The workflow runs under `card:<id>` so the projector can derive the
    /// `plan_key` from the workflow id; the caller passes the bare `id` around
    /// and `workflow_id` owns the prefix.
    pub(super) async fn start(&self, id: &str, claim: Claim) -> Handle {
        let legs = vec![LegSpec {
            id: "leg-1".to_string(),
            tier: Tier::Free,
            model: "some/model".to_string(),
            harness: "opencode".to_string(),
        }];
        self.start_with_legs(id, claim, legs).await
    }

    /// Like [`start`](Self::start), with the card's legs given by the caller.
    pub(super) async fn start_with_legs(
        &self,
        id: &str,
        claim: Claim,
        legs: Vec<LegSpec>,
    ) -> Handle {
        let input = CardInput {
            legacy: pg_worker::activities::CardInput {
                claim,
                legs,
                workroot: self.workroot.to_string_lossy().into_owned(),
                main_branch: "main".to_string(),
                question_wait_cap_s: self.question_cap_s.get(),
                max_question_rounds: self.max_rounds.get(),
            },
            risk_tagged: false,
            heldout_cmd: String::new(),
            smoke_checks: Vec::new(),
            exports: Vec::new(),
            base_sha: None,
            resume: None,
        };
        let wf_id = workflow_id(id);
        self.client
            .start_workflow(
                Card::run,
                input,
                WorkflowStartOptions::new(self.task_queue.as_str(), wf_id.as_str()).build(),
            )
            .await
            .expect("start card workflow");
        self.client.get_workflow_handle::<Card>(wf_id.as_str())
    }

    /// Cap how long the next card waits for an answer, so a test never sleeps 24 h.
    pub(super) fn set_question_cap(&self, secs: u64) {
        self.question_cap_s.set(Some(secs));
    }

    /// Questions the next card may have answered before it is parked.
    pub(super) fn set_max_rounds(&self, n: u32) {
        self.max_rounds.set(Some(n));
    }

    pub(super) async fn start_phase(
        &self,
        id: &str,
        input: PhaseInput,
    ) -> WorkflowHandle<Client, Phase> {
        let wf_id = format!("phase:{id}");
        self.client
            .start_workflow(
                Phase::run,
                input,
                WorkflowStartOptions::new(self.task_queue.as_str(), wf_id.as_str()).build(),
            )
            .await
            .expect("start phase workflow");
        self.client.get_workflow_handle::<Phase>(wf_id.as_str())
    }

    pub(super) fn phase_input(&self, key: &str, parallel: u32) -> PhaseInput {
        PhaseInput {
            max_waves: None,
            finish_poll_s: Some(1),
            finish_max_polls: Some(3),
            key: key.to_string(),
            repo: self.repo.display().to_string(),
            workroot: self.workroot.display().to_string(),
            base_branch: "main".to_string(),
            max_parallel: Some(parallel),
            legs: vec![LegSpec {
                id: "leg-1".to_string(),
                tier: Tier::Free,
                model: "some/model".to_string(),
                harness: "opencode".to_string(),
            }],
        }
    }

    pub(super) fn stop(&self) {
        if let Some(w) = self.worker.borrow().as_ref() {
            w.abort();
        }
    }

    /// Begin polling with a worker that was built but held back.
    pub(super) fn resume(&self) {
        if let Some(mut worker) = self.held.borrow_mut().take() {
            let handle = tokio::task::spawn_local(async move {
                let _ = worker.run().await;
            });
            *self.worker.borrow_mut() = Some(handle);
        }
    }

    /// Restarts the poll loop like a crash or a redeploy would: the old worker
    /// is aborted and a fresh one, with no memory of anything the old one
    /// held, starts polling the same task queue. The workflow server replays
    /// the history onto it, so only state carried in the history survives.
    pub(super) async fn restart_worker(&self) {
        self.stop();
        tokio::time::sleep(Duration::from_millis(300)).await;
        let opts = WorkerOptions::new(self.task_queue.as_str())
            .register_workflow::<Card>()
            .expect("register card workflow")
            .register_workflow::<Phase>()
            .expect("register phase workflow")
            .register_activities(CardActivities::new(self.deps.clone()))
            .build();
        let url = Url::from_str(&self.addr).expect("PG_TEMPORAL_ADDR parses as a url");
        let conn = Connection::connect(ConnectionOptions::new(url).build())
            .await
            .expect("connect restart worker client");
        let wclient =
            Client::new(conn, ClientOptions::new("default").build()).expect("restart client");
        let mut worker = Worker::new(&self._runtime, wclient, opts).expect("build restart worker");
        let handle = tokio::task::spawn_local(async move {
            let _ = worker.run().await;
        });
        *self.worker.borrow_mut() = Some(handle);
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        // Abort the poll loop before the temporary directories are removed.
        self.stop();
    }
}

/// The skip decision (D6 / I6'), extracted so it is testable without racing
/// the process env: with no `PG_TEMPORAL_ADDR` a test may skip only outside
/// CI; under `CI` a skip is a failure, so this panics instead.
pub(super) fn unique() -> u128 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed) as u128;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    (nanos << 64) ^ seq
}

/// Initialize a repo on `main` with a single commit holding `seed.txt`.
pub(super) fn init_repo(dir: &Path) -> String {
    git(dir, &["init", "-q", "-b", "main"]);
    // Persist the identity bits the worker's own git calls rely on.
    git(dir, &["config", "commit.gpgsign", "false"]);
    std::fs::write(dir.join("seed.txt"), "seed\n").expect("write seed.txt");
    git(dir, &["add", "-A"]);
    git(dir, &["commit", "-q", "-m", "init"]);
    git(dir, &["rev-parse", "HEAD"])
}

/// Local git runner, pinned identity and hermetic env like the other test
/// modules: worker-core's own helper is private to that crate.
pub(super) fn git(dir: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(dir)
        .args(args)
        .output()
        .expect("git runs");
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// Run a test body inside a `LocalSet`.
///
/// The Temporal worker's `run` future is not `Send`, so it can only be polled
/// from a local task; `LocalSet::run_until` gives one while still riding the
/// multi-thread test runtime.
pub(super) async fn in_local<F: std::future::Future>(f: F) -> F::Output {
    let local = tokio::task::LocalSet::new();
    local.run_until(f).await
}

fn workflow_id(id: &str) -> String {
    format!("card:{id}")
}
