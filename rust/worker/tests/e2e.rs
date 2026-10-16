//! End-to-end tests for the `card` workflow against a real Temporal dev server.
//!
//! These tests need a server reachable at `PG_TEMPORAL_ADDR` (for example
//! `http://127.0.0.1:17233`). When that variable is unset every test returns
//! early, so a checkout without a server still passes; CI that sets the
//! variable exercises the whole path: workflow, activities, git worktrees and
//! merges.
//!
//! Each test runs an in-process worker on a private task queue and drives one
//! card: the happy path, a contract defect, a check that never passes, and a
//! cancel signal. If `PG_RECORD_DIR` is set the fetched history is written
//! there as a fixture for the offline replay tests.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use pg_worker::activities::{CardActivities, CardInput, CardOutput, Deps, TASK_QUEUE};
use pg_worker::attempt::LegSpec;
use pg_worker::harness::{Harness, HarnessRun};
use pg_worker::keeper::{Claim, Keeper, StubKeeper};
use pg_worker::workflow::Card;
use pg_worker_core::config::{Config, Tier};
use pg_worker_core::exit_class::Patterns;
use tempfile::TempDir;
use temporalio_client::{
    Client, ClientOptions, Connection, ConnectionOptions, Url, WorkflowFetchHistoryOptions,
    WorkflowGetResultOptions, WorkflowHandle, WorkflowSignalOptions, WorkflowStartOptions,
};
use temporalio_sdk::{Runtime, Worker, WorkerOptions};

/// How long any single card may take before the test gives up.
const RESULT_TIMEOUT: Duration = Duration::from_secs(120);

type Handle = WorkflowHandle<Client, Card>;

/// Runs `sh -c <script>` inside the attempt's worktree, standing in for a real
/// agent CLI so a test can dictate exactly what the attempt does.
struct ShellHarness {
    script: String,
}

impl Harness for ShellHarness {
    fn name(&self) -> &str {
        "shell"
    }

    fn command(&self, run: &HarnessRun) -> Command {
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg(&self.script).current_dir(run.workdir);
        cmd
    }
}

/// One self-contained fixture: a real git repo, a workroot, a stub keeper, a
/// shell harness and a worker polling a private task queue.
struct Env {
    /// Kept only so the temporary directories outlive the worker.
    _repo: TempDir,
    repo: PathBuf,
    base_sha: String,
    _workroot: TempDir,
    workroot: PathBuf,
    keeper: Arc<StubKeeper>,
    client: Client,
    task_queue: String,
    worker: std::cell::RefCell<Option<tokio::task::JoinHandle<()>>>,
    /// A built worker not yet polling, for tests that signal before any worker runs.
    held: std::cell::RefCell<Option<Worker>>,
    _runtime: Runtime,
}

impl Env {
    async fn new(addr: &str, name: &str, script: &str, start: bool) -> Env {
        let repo = tempfile::tempdir().expect("repo tempdir");
        let repo_path = repo.path().to_path_buf();
        let base_sha = init_repo(&repo_path);
        let workroot = tempfile::tempdir().expect("workroot tempdir");
        let workroot_path = workroot.path().to_path_buf();

        let keeper = Arc::new(StubKeeper::default());
        let harness: Arc<dyn Harness> = Arc::new(ShellHarness {
            script: script.to_string(),
        });
        // These timeouts are the activities' (heartbeat cadence, stall kill);
        // the workflow keeps its own `Config::default()` for the retry policy,
        // so tests must not lean on workflow-side timeouts to finish.
        let cfg = Config {
            heartbeat: Duration::from_millis(200),
            heartbeat_timeout: Duration::from_secs(5),
            start_to_close: Duration::from_secs(60),
            stall: Duration::from_secs(30),
            ..Config::default()
        };
        // `Deps` wants a trait object; name the coercion explicitly so the
        // concrete `Arc<StubKeeper>` widens to `Arc<dyn Keeper>` here.
        let dyn_keeper: Arc<dyn Keeper> = keeper.clone();
        let deps = Deps {
            keeper: dyn_keeper,
            harness,
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
            .register_activities(CardActivities::new(deps))
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
            task_queue,
            worker: std::cell::RefCell::new(worker),
            held: std::cell::RefCell::new(held),
            _runtime: runtime,
        }
    }

    /// A claim against this fixture's repo, with the write set and check given
    /// by the caller.
    fn claim(&self, card: &str, allow: &[&str], check_cmd: &str) -> Claim {
        Claim {
            card: card.to_string(),
            repo: self.repo.to_string_lossy().into_owned(),
            base_sha: self.base_sha.clone(),
            check_cmd: check_cmd.to_string(),
            allow: allow.iter().map(|s| s.to_string()).collect(),
            forbid: Vec::new(),
            prompt: "do the thing".to_string(),
        }
    }

    /// Start the card workflow for `id` and hand back a typed handle.
    ///
    /// The workflow runs under `card:<id>` so the projector can derive the
    /// `plan_key` from the workflow id; the caller passes the bare `id` around
    /// and `workflow_id` owns the prefix.
    async fn start(&self, id: &str, claim: Claim) -> Handle {
        let input = CardInput {
            claim,
            legs: vec![LegSpec {
                id: "leg-1".to_string(),
                tier: Tier::Free,
                model: "some/model".to_string(),
            }],
            workroot: self.workroot.to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
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

    fn stop(&self) {
        if let Some(w) = self.worker.borrow().as_ref() {
            w.abort();
        }
    }

    /// Begin polling with a worker that was built but held back.
    fn resume(&self) {
        if let Some(mut worker) = self.held.borrow_mut().take() {
            let handle = tokio::task::spawn_local(async move {
                let _ = worker.run().await;
            });
            *self.worker.borrow_mut() = Some(handle);
        }
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        // Abort the poll loop before the temporary directories are removed.
        self.stop();
    }
}

/// `Some(Env)` when `PG_TEMPORAL_ADDR` is set, else `None` so the caller can
/// print the skip line and return.
async fn env_or_skip(name: &str, script: &str) -> Option<Env> {
    match std::env::var("PG_TEMPORAL_ADDR") {
        Ok(addr) if !addr.is_empty() => Some(Env::new(&addr, name, script, true).await),
        _ => None,
    }
}

/// Like `env_or_skip`, but the worker is built and held: nothing polls until
/// `Env::resume`, so a workflow and its signals queue up first.
async fn env_or_skip_paused(name: &str, script: &str) -> Option<Env> {
    match std::env::var("PG_TEMPORAL_ADDR") {
        Ok(addr) if !addr.is_empty() => Some(Env::new(&addr, name, script, false).await),
        _ => None,
    }
}

/// Await the card result under the shared deadline, then record its history.
async fn finish(name: &str, id: &str, handle: &Handle) -> CardOutput {
    let out = tokio::time::timeout(
        RESULT_TIMEOUT,
        handle.get_result(WorkflowGetResultOptions::default()),
    )
    .await
    .expect("card result within 120s")
    .expect("card workflow returns a result");
    record(name, id, handle).await;
    out
}

/// The workflow id for card `<id>`: the projector derives `plan_key` from it.
fn workflow_id(id: &str) -> String {
    format!("card:{id}")
}

/// Record this run as a fixture under `<PG_RECORD_DIR>`.
///
/// Two shapes are written: the SDK's JSON history as `<name>.json`, and, when
/// the temporal CLI is configured, its proto-JSON `workflow show` as
/// `<name>.proj.json` (the layout the Python projector reads). A missing CLI is
/// silent; a failing one panics, because a recording that quietly produces
/// nothing is worse than no recording at all.
async fn record(name: &str, id: &str, handle: &Handle) {
    let Ok(dir) = std::env::var("PG_RECORD_DIR") else {
        return;
    };
    if dir.is_empty() {
        return;
    }
    let Ok(bytes) = handle
        .fetch_history(WorkflowFetchHistoryOptions::default())
        .to_json()
        .await
    else {
        return;
    };
    let mut value: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => return,
    };
    scrub(&mut value);
    let json = serde_json::to_string(&value).unwrap_or_default();
    let dir = Path::new(&dir);
    if std::fs::create_dir_all(dir).is_ok() {
        let _ = std::fs::write(dir.join(format!("{name}.json")), json);
    }

    // The proto-JSON dump the projector consumes needs the CLI; `PG_TEMPORAL_CLI`
    // together with `PG_RECORD_DIR` opts in, anything else stays silent.
    let Ok(cli) = std::env::var("PG_TEMPORAL_CLI") else {
        return;
    };
    if cli.is_empty() {
        return;
    }
    let addr = std::env::var("PG_TEMPORAL_ADDR").unwrap_or_default();
    let address = addr.strip_prefix("http://").unwrap_or(&addr);
    let wf_id = workflow_id(id);
    let out = Command::new(cli.as_str())
        .arg("workflow")
        .arg("show")
        .arg("--workflow-id")
        .arg(wf_id.as_str())
        .arg("--address")
        .arg(address)
        .arg("--namespace")
        .arg("default")
        .arg("--output")
        .arg("json")
        .output()
        .unwrap_or_else(|e| panic!("temporal CLI {cli} failed to spawn: {e}"));
    assert!(
        out.status.success(),
        "temporal CLI workflow show failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let mut value: serde_json::Value = serde_json::from_slice(&out.stdout)
        .unwrap_or_else(|e| panic!("temporal CLI emitted invalid JSON: {e}"));
    scrub(&mut value);
    let json = serde_json::to_string(&value).unwrap_or_default();
    std::fs::write(dir.join(format!("{name}.proj.json")), json)
        .unwrap_or_else(|e| panic!("write {name}.proj.json: {e}"));
}

/// Replace host-specific identity strings (`pid@hostname`, sticky queue names)
/// so a recorded fixture carries no machine name.
fn scrub(v: &mut serde_json::Value) {
    match v {
        serde_json::Value::Object(m) => {
            for (k, x) in m.iter_mut() {
                if k == "identity" && x.as_str().is_some_and(|s| !s.is_empty()) {
                    *x = "e2e-worker".into();
                } else {
                    scrub(x);
                }
            }
        }
        serde_json::Value::Array(a) => a.iter_mut().for_each(scrub),
        serde_json::Value::String(s) => {
            // sticky queue names look like "<pid>@<hostname>-<uuid>"
            if let Some((pid, rest)) = s.split_once('@')
                && !pid.is_empty()
                && pid.bytes().all(|b| b.is_ascii_digit())
            {
                let uuid = rest.rsplit('-').next().unwrap_or("");
                *s = format!("1@e2e-{uuid}");
            }
        }
        _ => {}
    }
}

/// A process-wide counter mixed with the wall clock: unique within a run and
/// across quick successive runs.
fn unique() -> u128 {
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
fn init_repo(dir: &Path) -> String {
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

/// Run a test body inside a `LocalSet`.
///
/// The Temporal worker's `run` future is not `Send`, so it can only be polled
/// from a local task; `LocalSet::run_until` gives one while still riding the
/// multi-thread test runtime.
async fn in_local<F: std::future::Future>(f: F) -> F::Output {
    let local = tokio::task::LocalSet::new();
    local.run_until(f).await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn happy_attempt_merges_into_main() {
    in_local(async {
        let Some(env) = env_or_skip("happy", "echo ok > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-happy";
        let claim = env.claim(card, &["out.txt"], "test -f out.txt");
        let id = format!("e2e-happy-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("happy", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        let sha = out.merged_sha.clone().expect("a merged card has a sha");
        assert_eq!(git(&env.repo, &["rev-parse", "refs/heads/main"]), sha);
        assert!(
            env.keeper
                .completed()
                .iter()
                .any(|(c, s)| c == card && s == &sha),
            "keeper completed: {:?}",
            env.keeper.completed()
        );
        env.stop();
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn writing_outside_the_write_set_goes_to_planner() {
    in_local(async {
        let Some(env) = env_or_skip("contract", "echo evil > evil.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-contract";
        let claim = env.claim(card, &["out.txt"], "test -f out.txt");
        let id = format!("e2e-contract-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("contract", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(out.detail, "planner", "{out:?}");
        assert!(
            !env.keeper.parked().is_empty(),
            "a planner hand-off parks the card"
        );
        env.stop();
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_check_that_never_passes_stops_the_card() {
    in_local(async {
        let Some(env) = env_or_skip("check-fail", "echo ok > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-check-fail";
        let claim = env.claim(card, &["out.txt"], "exit 1");
        let id = format!("e2e-check-fail-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("check-fail", &id, &handle).await;
        let parked = out.outcome == "parked" && out.detail == "parked";
        let quarantined = out.outcome == "failed" && out.detail == "quarantined";
        assert!(parked || quarantined, "unexpected outcome {out:?}");
        assert!(
            !env.keeper.alarms().is_empty() || !env.keeper.parked().is_empty(),
            "a stuck card raises an alarm or a park packet"
        );
        env.stop();
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn cancel_signal_stops_a_running_attempt() {
    in_local(async {
        // The attempt sleeps for ten minutes, far past the deadline below, so a
        // pass can only mean the cancel signal killed it rather than the script
        // ending on its own.
        let marker = started_marker();
        let Some(env) = env_or_skip("cancel", &started_script(&marker)).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-cancel";
        let claim = env.claim(card, &["out.txt"], "test -f out.txt");
        let id = format!("e2e-cancel-{}", unique());
        let handle = env.start(&id, claim).await;

        // Signal only once the harness is really running, not after a guess.
        wait_for_marker(&marker).await;
        let started = std::time::Instant::now();
        handle
            .signal(
                Card::cancel,
                "operator".to_string(),
                WorkflowSignalOptions::default(),
            )
            .await
            .expect("send cancel signal");

        let out = tokio::time::timeout(
            Duration::from_secs(25),
            handle.get_result(WorkflowGetResultOptions::default()),
        )
        .await
        .expect("cancel takes effect within 25s")
        .expect("cancelled card returns a result");
        let elapsed = started.elapsed();
        record("cancel", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(out.detail, "cancelled", "{out:?}");
        assert!(
            elapsed < Duration::from_secs(25),
            "cancel took {elapsed:?}, expected under 25s"
        );
        env.stop();
    })
    .await;
}

/// A marker path the harness touches the moment it starts running.
fn started_marker() -> PathBuf {
    std::env::temp_dir().join(format!("pg-e2e-started-{}", unique()))
}

fn started_script(marker: &Path) -> String {
    format!("touch '{}'; sleep 600", marker.display())
}

/// Poll until the harness has touched `marker`.
async fn wait_for_marker(marker: &Path) {
    let deadline = std::time::Instant::now() + Duration::from_secs(60);
    while !marker.exists() {
        assert!(
            std::time::Instant::now() < deadline,
            "attempt never started (no marker)"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Send `cancel` and require a `cancelled` detail inside the budget.
async fn cancel_and_expect(env: &Env, handle: &Handle) {
    let started = std::time::Instant::now();
    handle
        .signal(
            Card::cancel,
            "operator".to_string(),
            WorkflowSignalOptions::default(),
        )
        .await
        .expect("send cancel signal");
    let out = tokio::time::timeout(
        Duration::from_secs(25),
        handle.get_result(WorkflowGetResultOptions::default()),
    )
    .await
    .expect("cancel takes effect within 25s")
    .expect("cancelled card returns a result");
    assert_eq!(out.outcome, "parked", "{out:?}");
    assert_eq!(out.detail, "cancelled", "{out:?}");
    assert!(started.elapsed() < Duration::from_secs(25));
    env.stop();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn cancel_immediately_after_start_is_honoured() {
    in_local(async {
        let marker = started_marker();
        let Some(env) = env_or_skip("cancel-now", &started_script(&marker)).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let claim = env.claim("e2e-cancel-now", &["out.txt"], "test -f out.txt");
        let handle = env
            .start(&format!("e2e-cancel-now-{}", unique()), claim)
            .await;
        // No settle at all: the signal races the workflow's first tasks.
        cancel_and_expect(&env, &handle).await;
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn cancel_while_the_first_activity_is_pending_is_honoured() {
    in_local(async {
        let marker = started_marker();
        let Some(env) = env_or_skip_paused("cancel-pending", &started_script(&marker)).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let claim = env.claim("e2e-cancel-pending", &["out.txt"], "test -f out.txt");
        let handle = env
            .start(&format!("e2e-cancel-pending-{}", unique()), claim)
            .await;
        // Nothing polls yet, so the first activity is queued and unstarted when the
        // signal lands; the worker only comes up afterwards.
        handle
            .signal(
                Card::cancel,
                "operator".to_string(),
                WorkflowSignalOptions::default(),
            )
            .await
            .expect("send cancel signal");
        env.resume();
        let out = tokio::time::timeout(
            Duration::from_secs(25),
            handle.get_result(WorkflowGetResultOptions::default()),
        )
        .await
        .expect("cancel takes effect within 25s")
        .expect("cancelled card returns a result");
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(out.detail, "cancelled", "{out:?}");
        assert!(!marker.exists(), "no attempt may start after the cancel");
        env.stop();
    })
    .await;
}
