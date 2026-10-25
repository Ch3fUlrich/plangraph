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
//!
//! Live conditions: the park packet binds to the principal NAME, so the Rust
//! ParkPacket must send the claim epoch, or each worker process must run under
//! its own principal; shared-principal locks need a unique owner per
//! process/run (judge note, not exercised by these tests). Gate tests: the
//! card check must pass inside the attempt and fail when the gate re-runs it,
//! hence the counter-file check.

#![cfg(unix)]

use pg_worker_core::exit_class::ExitClass;
use pg_worker_core::redact::REDACTED;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use pg_worker::activities::{CardActivities, CardOutput, Deps, TASK_QUEUE};
use pg_worker::attempt::LegSpec;
use pg_worker::harness::{Harness, HarnessRegistry, HarnessRun};
use pg_worker::keeper::{Claim, Keeper, StubKeeper};
use pg_worker::phase::{PhaseInput, PhaseOutput};
use pg_worker::phase_workflow::Phase;
use pg_worker::workflow::{Card, CardInput};
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

/// How long a cancel may take to land on the shared, loaded CI host.
///
/// Above any plausible cancel latency on a loaded shared CI host, yet below the
/// 300 s attempt timeout so a slow host cannot end the attempt by timeout; the
/// harness prints a line every second, so the 30 s stall watchdog never fires
/// either. Finishing inside this budget can therefore only mean the cancel
/// reached the child.
const CANCEL_BUDGET: Duration = Duration::from_secs(150);

type Handle = WorkflowHandle<Client, Card>;

/// Runs `sh -c <script>` inside the attempt's worktree, standing in for a real
/// agent CLI so a test can dictate exactly what the attempt does. The leg's
/// model reaches the script as `$PG_MODEL`, so one script can behave
/// differently per leg (for example, hit a limit on one model only).
struct ShellHarness {
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
struct Env {
    /// Kept only so the temporary directories outlive the worker.
    _repo: TempDir,
    repo: PathBuf,
    base_sha: String,
    _workroot: TempDir,
    workroot: PathBuf,
    keeper: Arc<StubKeeper>,
    client: Client,
    /// The activities' dependencies, kept so a restarted worker can be built
    /// with the same keeper, harnesses and config.
    deps: Deps,
    /// The `PG_TEMPORAL_ADDR` url, kept so a restarted worker can reconnect.
    addr: String,
    task_queue: String,
    worker: std::cell::RefCell<Option<tokio::task::JoinHandle<()>>>,
    /// A built worker not yet polling, for tests that signal before any worker runs.
    held: std::cell::RefCell<Option<Worker>>,
    /// Question wait cap handed to the next card, in seconds; unset = 24 h.
    question_cap_s: std::cell::Cell<Option<u64>>,
    max_rounds: std::cell::Cell<Option<u32>>,
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
            ..Config::default()
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
    fn claim(&self, card: &str, allow: &[&str], check_cmd: &str) -> Claim {
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
    async fn start(&self, id: &str, claim: Claim) -> Handle {
        let legs = vec![LegSpec {
            id: "leg-1".to_string(),
            tier: Tier::Free,
            model: "some/model".to_string(),
            harness: "opencode".to_string(),
        }];
        self.start_with_legs(id, claim, legs).await
    }

    /// Like [`start`](Self::start), with the card's legs given by the caller.
    async fn start_with_legs(&self, id: &str, claim: Claim, legs: Vec<LegSpec>) -> Handle {
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
    fn set_question_cap(&self, secs: u64) {
        self.question_cap_s.set(Some(secs));
    }

    /// Questions the next card may have answered before it is parked.
    fn set_max_rounds(&self, n: u32) {
        self.max_rounds.set(Some(n));
    }

    async fn start_phase(&self, id: &str, input: PhaseInput) -> WorkflowHandle<Client, Phase> {
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

    fn phase_input(&self, key: &str, parallel: u32) -> PhaseInput {
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

    /// Restarts the poll loop like a crash or a redeploy would: the old worker
    /// is aborted and a fresh one, with no memory of anything the old one
    /// held, starts polling the same task queue. The workflow server replays
    /// the history onto it, so only state carried in the history survives.
    async fn restart_worker(&self) {
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

/// `(combo, exit_class)` of every finished `attempt` activity, in order, read
/// from the workflow history. This is the same payload the projector reads.
async fn attempt_outcomes(handle: &Handle) -> Vec<(String, String)> {
    let bytes = handle
        .fetch_history(WorkflowFetchHistoryOptions::default())
        .to_json()
        .await
        .expect("fetch history json");
    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("history is json");
    let mut out = Vec::new();
    for ev in value["events"].as_array().expect("events array") {
        let Some(data) = ev["attributes"]["ActivityTaskCompletedEventAttributes"]["result"]
            ["payloads"][0]["data"]
            .as_str()
        else {
            continue;
        };
        let Ok(result) = serde_json::from_slice::<serde_json::Value>(&b64_decode(data)) else {
            continue;
        };
        if let (Some(combo), Some(class)) =
            (result["combo"].as_str(), result["exit_class"].as_str())
        {
            out.push((combo.to_string(), class.to_string()));
        }
    }
    out
}

/// How many times the workflow *scheduled* activities named `name`: one
/// `ActivityTaskScheduled` event per workflow command, so an activity Temporal
/// retried server-side counts once -- which is exactly what separates D1
/// retries from workflow-level re-observe loops.
async fn scheduled_activities(handle: &Handle, name: &str) -> usize {
    let bytes = handle
        .fetch_history(WorkflowFetchHistoryOptions::default())
        .to_json()
        .await
        .expect("fetch history json");
    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("history is json");
    value["events"]
        .as_array()
        .expect("events array")
        .iter()
        .filter(|ev| {
            ev["attributes"]["ActivityTaskScheduledEventAttributes"]["activity_type"]["name"]
                .as_str()
                == Some(name)
        })
        .count()
}

/// Standard base64 (padding optional); enough to read history payloads
/// without a crate. Invalid characters are skipped.
fn b64_decode(s: &str) -> Vec<u8> {
    let mut out = Vec::new();
    let (mut acc, mut bits) = (0u32, 0u32);
    for c in s.bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => continue,
        };
        acc = (acc << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    out
}

/// Every payload in a history, base64-decoded: the claim input, activity
/// inputs and activity results all travel as payloads, which the `to_json`
/// form stores base64-encoded — a raw substring search cannot see into them.
fn decoded_payloads(v: &serde_json::Value) -> Vec<String> {
    match v {
        serde_json::Value::Object(map) => {
            let mut out = Vec::new();
            for (k, x) in map {
                if k == "payloads"
                    && let Some(items) = x.as_array()
                {
                    for p in items {
                        if let Some(data) = p.get("data").and_then(|d| d.as_str()) {
                            out.push(String::from_utf8_lossy(&b64_decode(data)).into_owned());
                        }
                    }
                }
                out.extend(decoded_payloads(x));
            }
            out
        }
        serde_json::Value::Array(a) => a.iter().flat_map(decoded_payloads).collect(),
        _ => Vec::new(),
    }
}

/// The workflow id for card `<id>`: the projector derives `plan_key` from it.
fn workflow_id(id: &str) -> String {
    format!("card:{id}")
}

/// Every keeper call the card made must carry the full execution identity:
/// the workflow id, this run's id and the card attempt number (1-based, never
/// Temporal's activity retry count). Calls name an attempt the card has run,
/// in non-decreasing order: a question-timeout alarm can precede another attempt.
fn assert_execs_name_the_attempts(env: &Env, attempts: u32) {
    let execs = env.keeper.execs();
    assert!(
        !execs.is_empty(),
        "the card must have reached the keeper at least once"
    );
    let mut previous = 0;
    for exec in &execs {
        assert!(
            exec.workflow_id.is_some(),
            "every keeper call carries the workflow id: {exec:?}"
        );
        assert!(
            exec.run_id.is_some(),
            "every keeper call carries the run id: {exec:?}"
        );
        let attempt = exec
            .attempt
            .unwrap_or_else(|| panic!("every keeper call carries the card attempt: {exec:?}"));
        assert!(attempt >= 1, "the card attempt is 1-based: {exec:?}");
        assert!(
            attempt <= attempts,
            "keeper call cannot name a future attempt: {exec:?}"
        );
        assert!(
            attempt >= previous,
            "keeper call attempts cannot go backwards: {exec:?}"
        );
        previous = attempt;
    }
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
        // The card merged on its very first attempt, and the keeper's `complete`
        // call names that attempt (with the workflow and run ids).
        assert_eq!(out.attempts, 1, "{out:?}");
        assert_execs_name_the_attempts(&env, out.attempts);
        env.stop();
    })
    .await;
}

/// D-1533: no hard Claude dependency. The first leg is a Claude-family leg that
/// hits its usage window; the card must continue on the other family's leg
/// without counting that as an attempt, and still merge.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn usage_limit_reroutes_to_non_claude_leg() {
    in_local(async {
        let script = r#"if [ "$PG_MODEL" = "claude-ish/model" ]; then echo "5-hour limit reached"; exit 1; fi; echo ok > out.txt"#;
        let Some(env) = env_or_skip("usage-limit", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-usage-limit";
        let claim = env.claim(card, &["out.txt"], "test -f out.txt");
        let id = format!("e2e-usage-limit-{}", unique());
        let legs = vec![
            LegSpec {
                id: "claude-leg".to_string(),
                tier: Tier::Free,
                model: "claude-ish/model".to_string(),
                harness: "claude".to_string(),
            },
            LegSpec {
                id: "open-leg".to_string(),
                tier: Tier::Free,
                model: "open/model".to_string(),
                harness: "opencode".to_string(),
            },
        ];
        let handle = env.start_with_legs(&id, claim, legs).await;

        let out = finish("usage_limit_reroute", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        assert_eq!(
            attempt_outcomes(&handle).await,
            vec![
                ("claude-leg".to_string(), "usage_limit".to_string()),
                ("open-leg".to_string(), "ok".to_string()),
            ]
        );
        // The provider-limited attempt did not consume budget: the keeper saw
        // it as `usage_limit` (a class the budget never counts), and the very
        // next attempt ran as an ordinary first try on the other leg, with no
        // retry, promotion or wait in between.
        let reports = env.keeper.attempts();
        let seen: Vec<(u32, String, ExitClass)> = reports
            .iter()
            .map(|r| (r.n, r.leg.clone(), r.exit_class))
            .collect();
        assert_eq!(
            seen,
            vec![
                (1, "claude-leg".to_string(), ExitClass::UsageLimit),
                (2, "open-leg".to_string(), ExitClass::Ok),
            ],
            "{reports:?}"
        );
        assert_eq!(out.attempts, 2, "{out:?}");
        let sha = out.merged_sha.clone().expect("a merged card has a sha");
        assert_eq!(git(&env.repo, &["rev-parse", "refs/heads/main"]), sha);
        env.stop();
    })
    .await;
}

/// Wait until the stub keeper holds `count` questions; returns the newest id
/// (the stub numbers questions from 1).
async fn wait_for_questions(env: &Env, count: usize) -> i64 {
    let deadline = std::time::Instant::now() + Duration::from_secs(60);
    while env.keeper.questions().len() < count {
        assert!(
            std::time::Instant::now() < deadline,
            "the card never asked its question"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    count as i64
}

/// The agent asks once, then finishes only when its prompt carries `needle`.
fn ask_then_work(packet: &str, needle: &str) -> String {
    format!(
        r#"case "$PG_PROMPT" in *"{needle}"*) echo ok > out.txt;; *) echo 'PG_QUESTION: {packet}';; esac"#
    )
}

/// RT-P1 inbox: a question parks nothing. The card asks, waits durably, takes
/// the inbox `answer` signal, and the next attempt (no retry budget spent)
/// carries the answer in its prompt and merges.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_answer_signal_resumes() {
    in_local(async {
        // Every attempt appends its working directory next to the repo, so the
        // test can see the answered attempt did not reuse the asking one's.
        let script = format!(
            r#"R=$(dirname "$(git rev-parse --git-common-dir)"); echo "$PWD" >> "$R/cwds.txt"
{}"#,
            ask_then_work(r#"{"text":"which db?"}"#, "use sqlite")
        );
        let Some(env) = env_or_skip("needs-input-answer", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let claim = env.claim("e2e-answer", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-answer-{}", unique());
        let handle = env.start(&id, claim).await;

        let qid = wait_for_questions(&env, 1).await;
        // The shape the inbox sends (src/plangraph/inbox/temporal_signal.py).
        handle
            .signal(
                Card::answer,
                serde_json::json!({
                    "question_id": qid,
                    "key": "demo:A",
                    "text": "use sqlite",
                    "source": "answer",
                }),
                WorkflowSignalOptions::default(),
            )
            .await
            .expect("send answer signal");

        let out = finish("needs_input_answered", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        assert_eq!(
            attempt_outcomes(&handle).await,
            vec![
                ("leg-1".to_string(), "needs_input".to_string()),
                ("leg-1".to_string(), "ok".to_string()),
            ]
        );
        assert_eq!(env.keeper.questions().len(), 1);
        assert!(env.keeper.question_timeouts().is_empty());
        // The answered attempt is a new attempt: a fresh number, a fresh worktree.
        let ns: Vec<u32> = env.keeper.attempts().iter().map(|a| a.n).collect();
        assert_eq!(ns, vec![1, 2], "attempt numbers must not repeat");
        let cwds = std::fs::read_to_string(env.repo.join("cwds.txt")).unwrap();
        let cwds: Vec<&str> = cwds.lines().collect();
        assert_eq!(cwds.len(), 2, "{cwds:?}");
        assert_ne!(cwds[0], cwds[1], "the answered attempt reused a worktree");
        let sha = out.merged_sha.clone().expect("a merged card has a sha");
        assert_eq!(git(&env.repo, &["rev-parse", "refs/heads/main"]), sha);
        env.stop();
    })
    .await;
}

/// No answer before the deadline and a default on offer: the workflow timer
/// records `question_timeout` and the card continues with the default.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_default_after_deadline() {
    in_local(async {
        let script = ask_then_work(
            r#"{"text":"which db?","default":"go with sqlite","deadline_s":2,"risk_tags":[]}"#,
            "go with sqlite",
        );
        let Some(env) = env_or_skip("needs-input-timeout", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let claim = env.claim("e2e-timeout", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-timeout-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_timeout", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        assert_eq!(env.keeper.question_timeouts(), vec![1]);
        let asked = env.keeper.questions();
        assert_eq!(asked[0].1.default.as_deref(), Some("go with sqlite"));
        assert_eq!(asked[0].1.deadline_s, Some(2));
        assert!(env.keeper.alarms().is_empty(), "{:?}", env.keeper.alarms());
        env.stop();
    })
    .await;
}

/// A default whose deadline lies far beyond the wait cap: the deadline sent to
/// the keeper is clamped to the cap, so the workflow timer and the keeper's
/// stored deadline are the same instant and the timeout is recorded (it never
/// parks while the keeper still holds a default).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_deadline_past_the_cap_is_clamped() {
    in_local(async {
        let script = ask_then_work(
            r#"{"text":"which db?","default":"go with sqlite","deadline_s":86400,"risk_tags":[]}"#,
            "go with sqlite",
        );
        let Some(env) = env_or_skip("needs-input-clamped", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.set_question_cap(3);
        let claim = env.claim("e2e-clamped", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-clamped-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_clamped", &id, &handle).await;
        assert_eq!(out.detail, "merged", "{out:?}");
        let asked = env.keeper.questions();
        assert_eq!(asked[0].1.deadline_s, Some(3), "{asked:?}");
        assert_eq!(asked[0].1.default.as_deref(), Some("go with sqlite"));
        assert_eq!(env.keeper.question_timeouts(), vec![1]);
        env.stop();
    })
    .await;
}

/// A keeper that refuses to record the timeout (worker token without the
/// router or system role) is alarmed, and the card still takes the default.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_default_continues_when_the_timeout_is_refused() {
    in_local(async {
        let script = ask_then_work(
            r#"{"text":"which db?","default":"go with sqlite","deadline_s":1}"#,
            "go with sqlite",
        );
        let Some(env) = env_or_skip("needs-input-refused", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.keeper.set_timeout_refused(true);
        let claim = env.claim("e2e-refused", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-refused-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_timeout_refused", &id, &handle).await;
        assert_eq!(out.detail, "merged", "{out:?}");
        assert_eq!(env.keeper.question_timeouts(), vec![1]);
        let alarms = env.keeper.alarms();
        assert!(
            alarms
                .iter()
                .any(|a| a.reason == "question_timeout not recorded"),
            "{alarms:?}"
        );
        env.stop();
    })
    .await;
}

/// A risk-tagged question has no default even if the agent offered one: the
/// card waits for an answer or cancel, and parks with an alarm at the (here
/// injected) wait cap. The keeper never sees a timeout.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_risk_tagged_never_defaults() {
    in_local(async {
        let script = r#"echo 'PG_QUESTION: {"text":"spend money?","default":"yes","deadline_s":1,"risk_tags":["money"]}'"#;
        let Some(env) = env_or_skip("needs-input-risk", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.set_question_cap(4);
        let claim = env.claim("e2e-risk", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-risk-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_risk_tagged", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert!(env.keeper.question_timeouts().is_empty());
        let asked = env.keeper.questions();
        assert_eq!(asked.len(), 1);
        assert_eq!(
            asked[0].1.default, None,
            "the keeper must not be sent a default"
        );
        assert_eq!(asked[0].1.risk_tags, vec!["money".to_string()]);
        assert!(
            env.keeper
                .alarms()
                .iter()
                .any(|a| a.reason == "question_timeout"),
            "{:?}",
            env.keeper.alarms()
        );
        let parked = env.keeper.parked();
        assert_eq!(parked.len(), 1);
        assert!(
            parked
                .iter()
                .filter(|p| p.check_tail.starts_with("question_timeout: "))
                .count()
                == 1,
            "{parked:?}"
        );
        // The question path too: ask, park and alarm all name the attempt that
        // asked, here the card's only attempt.
        assert_eq!(out.attempts, 1, "{out:?}");
        assert_execs_name_the_attempts(&env, out.attempts);
        env.stop();
    })
    .await;
}

/// A worker restart during the wait: the restart drops all in-memory worker
/// state, so only the durable timer in the history can fire the park. The card
/// parks on the question timeout and the keeper is never asked twice.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_restart_mid_wait_parks_question_timeout() {
    in_local(async {
        let script = r#"echo 'PG_QUESTION: {"text":"which db?"}'"#;
        let Some(env) = env_or_skip("needs-input-restart-park", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.set_question_cap(6);
        let claim = env.claim("e2e-restart-park", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-restart-park-{}", unique());
        let handle = env.start(&id, claim).await;

        wait_for_questions(&env, 1).await;
        env.restart_worker().await;

        let out = finish("needs_input_restart_park", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert!(
            env.keeper
                .alarms()
                .iter()
                .any(|a| a.reason == "question_timeout"),
            "{:?}",
            env.keeper.alarms()
        );
        let parked = env.keeper.parked();
        assert_eq!(parked.len(), 1);
        assert_eq!(parked[0].check_tail, "question_timeout: which db?");
        assert_eq!(
            env.keeper.questions().len(),
            1,
            "the question is not asked again after the restart"
        );
        assert!(env.keeper.question_timeouts().is_empty());
        env.stop();
    })
    .await;
}

/// The same restart with a default on offer: the replayed history re-arms the
/// durable timer, it fires on the restarted worker, and the card takes the
/// default and merges.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_restart_mid_wait_takes_the_default() {
    in_local(async {
        let script = ask_then_work(
            r#"{"text":"which db?","default":"go with sqlite","deadline_s":6,"risk_tags":[]}"#,
            "go with sqlite",
        );
        let Some(env) = env_or_skip("needs-input-restart-default", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let claim = env.claim("e2e-restart-default", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-restart-default-{}", unique());
        let handle = env.start(&id, claim).await;

        wait_for_questions(&env, 1).await;
        env.restart_worker().await;

        let out = finish("needs_input_restart_default", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        assert_eq!(env.keeper.question_timeouts(), vec![1]);
        assert_eq!(env.keeper.questions().len(), 1);
        assert!(env.keeper.alarms().is_empty(), "{:?}", env.keeper.alarms());
        env.stop();
    })
    .await;
}

/// A card that keeps asking is bounded: with the limit at 2, the third
/// `needs_input` parks it with alarm `question rounds exhausted` and the keeper
/// is never asked a third question.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_rounds_are_bounded() {
    in_local(async {
        // Never satisfied: it asks on every attempt.
        let script = r#"echo 'PG_QUESTION: {"text":"again?"}'"#;
        let Some(env) = env_or_skip("needs-input-rounds", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.set_max_rounds(2);
        let claim = env.claim("e2e-rounds", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-rounds-{}", unique());
        let handle = env.start(&id, claim).await;

        for round in 1..=2 {
            let qid = wait_for_questions(&env, round).await;
            handle
                .signal(
                    Card::answer,
                    serde_json::json!({
                        "question_id": qid, "key": "demo:A", "text": "ok", "source": "answer",
                    }),
                    WorkflowSignalOptions::default(),
                )
                .await
                .expect("send answer signal");
        }
        let out = finish("needs_input_rounds", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(
            env.keeper.questions().len(),
            2,
            "no third question is asked"
        );
        assert!(
            env.keeper
                .alarms()
                .iter()
                .any(|a| a.reason == "question rounds exhausted"),
            "{:?}",
            env.keeper.alarms()
        );
        assert_eq!(env.keeper.parked().len(), 1);
        assert_eq!(env.keeper.parked()[0].check_tail, "again?");
        env.stop();
    })
    .await;
}

/// An answer for another question, an unparsable payload and a payload with
/// extra fields and no `key`/`source` never wedge the card: only the matching
/// one is consumed.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_tolerates_odd_answer_signals() {
    in_local(async {
        let script = ask_then_work(r#"{"text":"which db?"}"#, "use sqlite");
        let Some(env) = env_or_skip("needs-input-odd-signals", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let claim = env.claim("e2e-odd", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-odd-{}", unique());
        let handle = env.start(&id, claim).await;
        let qid = wait_for_questions(&env, 1).await;
        for payload in [
            serde_json::json!({"question_id": qid + 40, "text": "use postgres"}),
            serde_json::json!("not an object"),
            serde_json::json!({"question_id": "x", "text": 5}),
            serde_json::json!({"question_id": qid, "text": "use sqlite", "extra": [1, 2]}),
        ] {
            handle
                .signal(Card::answer, payload, WorkflowSignalOptions::default())
                .await
                .expect("send answer signal");
        }
        let out = finish("needs_input_odd_signals", &id, &handle).await;
        assert_eq!(out.detail, "merged", "{out:?}");
        assert_eq!(env.keeper.questions().len(), 1);
        env.stop();
    })
    .await;
}

/// A keeper outage on `record_question_timeout` (5xx, after the in-activity
/// retries) and a dead alarm sink must not fail the card: it takes the default.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_default_survives_keeper_and_alarm_outage() {
    in_local(async {
        let script = ask_then_work(
            r#"{"text":"which db?","default":"go with sqlite","deadline_s":1}"#,
            "go with sqlite",
        );
        let Some(env) = env_or_skip("needs-input-outage", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.keeper.set_timeout_status(Some(503));
        env.keeper.set_alarm_down(true);
        let claim = env.claim("e2e-outage", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-outage-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_outage", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        // One call plus the bounded transient retries.
        assert_eq!(env.keeper.question_timeouts().len(), 4);
        env.stop();
    })
    .await;
}

/// The `record_question_timeout` call dying outright (not a keeper error: the
/// client panics) must not fail the card either: alarm best-effort, continue
/// with the default.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_default_continues_when_the_timeout_call_dies() {
    in_local(async {
        let script = ask_then_work(
            r#"{"text":"which db?","default":"go with sqlite","deadline_s":1}"#,
            "go with sqlite",
        );
        let Some(env) = env_or_skip("needs-input-timeout-dies", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.keeper.set_timeout_panics(true);
        let claim = env.claim("e2e-dies", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-dies-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_timeout_dies", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        assert!(!env.keeper.question_timeouts().is_empty());
        assert!(
            env.keeper
                .alarms()
                .iter()
                .any(|a| a.reason == "question_timeout not recorded"),
            "{:?}",
            env.keeper.alarms()
        );
        env.stop();
    })
    .await;
}

/// A keeper outage on `ask_question` (5xx, after every retry) parks the card
/// with an alarm instead of failing the workflow.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_ask_outage_parks_with_an_alarm() {
    in_local(async {
        let script = r#"echo 'PG_QUESTION: {"text":"which db?"}'"#;
        let Some(env) = env_or_skip("needs-input-ask-outage", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.keeper.set_ask_status(Some(503));
        let claim = env.claim("e2e-ask-outage", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-ask-outage-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_ask_outage", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert!(env.keeper.questions().is_empty());
        let alarms = env.keeper.alarms();
        assert!(
            alarms
                .iter()
                .any(|a| a.reason.starts_with("question not recorded")),
            "{alarms:?}"
        );
        assert_eq!(env.keeper.parked().len(), 1);
        assert_eq!(env.keeper.parked()[0].check_tail, "which db?");
        env.stop();
    })
    .await;
}

/// A keeper that refuses the question outright (403: retrying cannot help)
/// parks the card with the `question not recorded` alarm and the question text
/// as the packet's check tail.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn needs_input_ask_refused_parks_with_question_not_recorded() {
    in_local(async {
        let script = r#"echo 'PG_QUESTION: {"text":"which db?"}'"#;
        let Some(env) = env_or_skip("needs-input-ask-refused", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        env.keeper.set_ask_status(Some(403));
        let claim = env.claim("e2e-ask-refused", &["out.txt"], "test -f out.txt");
        let id = format!("e2e-ask-refused-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("needs_input_ask_refused", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        let alarms = env.keeper.alarms();
        assert!(
            alarms.iter().any(|a| a.reason == "question not recorded"),
            "{alarms:?}"
        );
        assert_eq!(env.keeper.parked().len(), 1);
        assert_eq!(env.keeper.parked()[0].check_tail, "which db?");
        env.stop();
    })
    .await;
}

/// Shell preamble that finds the shared repo from inside an attempt worktree
/// (the common git dir is absolute there) and lands a commit on `main` while
/// the attempt is still running, so the merge sees a moved base.
const MOVE_MAIN: &str = r#"R=$(dirname "$(git rev-parse --git-common-dir)")
if [ ! -f "$R/moved.txt" ]; then
  echo m > "$R/moved.txt"
  git -C "$R" add moved.txt
  git -C "$R" -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false commit -q -m moved
fi
"#;

/// Main advances (a different file) between the attempt's base and its merge:
/// the worker rebases the attempt onto the new main, re-runs the check once and
/// fast-forwards, all in the same attempt.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn merge_base_moved_rebases_and_merges() {
    in_local(async {
        let script = format!("{MOVE_MAIN}echo ok > out.txt");
        let Some(env) = env_or_skip("base-moved", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-base-moved";
        let claim = env.claim(card, &["out.txt"], "test -f out.txt");
        let id = format!("e2e-base-moved-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("base_moved_rebase", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        // One attempt: the move was absorbed by the rebase, not a new attempt.
        assert_eq!(attempt_outcomes(&handle).await.len(), 1);
        let sha = out.merged_sha.clone().expect("a merged card has a sha");
        assert_eq!(git(&env.repo, &["rev-parse", "refs/heads/main"]), sha);
        // Both the moved commit and the attempt's file are on main.
        assert_eq!(git(&env.repo, &["show", "main:moved.txt"]), "m");
        assert_eq!(git(&env.repo, &["show", "main:out.txt"]), "ok");
        assert_eq!(git(&env.repo, &["rev-parse", "main~1"]).len(), 40);
        // The keeper merge lock was taken and returned.
        assert!(!env.keeper.merge_lock_held());
        assert_eq!(env.keeper.lock_calls().len(), 2);
        env.stop();
    })
    .await;
}

/// Main edits the very line the attempt edits: the rebase conflicts, nothing is
/// merged from that attempt, and the card requeues a fresh attempt on the new
/// base, which then merges.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn merge_rebase_conflict_requeues() {
    in_local(async {
        let script = r#"R=$(dirname "$(git rev-parse --git-common-dir)")
if ! grep -q '^m$' "$R/seed.txt"; then
  echo m > "$R/seed.txt"
  git -C "$R" add seed.txt
  git -C "$R" -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false commit -q -m moved
fi
echo attempt > seed.txt"#;
        let Some(env) = env_or_skip("rebase-conflict", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-rebase-conflict";
        let mut claim = env.claim(card, &["seed.txt"], "test -f seed.txt");
        claim.check_kind = "regression".into(); // seed.txt already exists on base.
        let id = format!("e2e-rebase-conflict-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("rebase_conflict_requeue", &id, &handle).await;
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        assert_eq!(
            attempt_outcomes(&handle).await.len(),
            2,
            "a conflicting rebase requeues one more attempt"
        );
        let sha = out.merged_sha.clone().expect("a merged card has a sha");
        assert_eq!(git(&env.repo, &["rev-parse", "refs/heads/main"]), sha);
        assert_eq!(git(&env.repo, &["show", "main:seed.txt"]), "attempt");
        env.stop();
    })
    .await;
}

/// A check that passes in the attempt's worktree but fails once the attempt is
/// rebased onto the moved main: the merge-time verdict replaces the attempt in
/// workflow history, and the keeper must be told the same (attempt 1 reported
/// `ok`, then re-reported `check_fail`).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn merge_time_check_fail_is_re_reported_to_the_keeper() {
    in_local(async {
        let script = format!("{MOVE_MAIN}echo ok > out.txt");
        let Some(env) = env_or_skip("merge-check-fail", &script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-merge-check-fail";
        let claim = env.claim(card, &["out.txt"], "test -f out.txt && ! test -f moved.txt");
        let id = format!("e2e-merge-check-fail-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("merge_check_fail", &id, &handle).await;
        assert_ne!(out.detail, "merged", "{out:?}");
        let first: Vec<ExitClass> = env
            .keeper
            .attempts()
            .iter()
            .filter(|r| r.n == 1)
            .map(|r| r.exit_class)
            .collect();
        assert_eq!(
            first,
            vec![ExitClass::Ok, ExitClass::CheckFail],
            "attempt 1 is reported ok, then corrected to the merge-time verdict"
        );
        env.stop();
    })
    .await;
}

/// The check command both gate tests share: a counter that tells its own
/// invocations apart, so the SAME command passes inside the attempt and fails
/// when the pre-merge gate re-runs it on the exact tree about to land. Every
/// invocation reads the count from `$R/chk.n` in the shared repo root — found
/// from inside an attempt worktree exactly as MOVE_MAIN finds it — and
/// overwrites it with the incremented value; when the pre-increment count `N`
/// meets `fail_when`, `on_fail` runs and its exit status is the check's.
/// Filesystem only: the check's environment is secret-free, so the attempt's
/// run (invocation 0, 2, ...) and the gate's run (1, 3, ...) can only differ
/// through this file. `on_fail` must `exit` non-zero itself; a count that
/// does not meet `fail_when` passes.
fn counter_check(fail_when: &str, on_fail: &str) -> String {
    format!(
        r#"R=$(dirname "$(git rev-parse --git-common-dir)")
N=$(cat "$R/chk.n" 2>/dev/null || echo 0)
echo $((N + 1)) > "$R/chk.n"
if {fail_when}; then
{on_fail}
fi"#
    )
}

/// The card's check gates the merge: the first attempt's tree passes the
/// check inside the attempt, and the gate's re-run of the SAME command fails
/// (see `counter_check`), which blocks the merge before anything lands — so
/// main stays exactly where that attempt found it, and the retry — the same
/// stateful harness script, now writing the passing tree — merges straight on
/// top of that same base. The harness script is identical for every attempt,
/// so its state lives in the shared repo (like MOVE_MAIN's): it appends main's
/// ref to `main-at-start.txt` at the start of every attempt, which is what
/// lets the test prove main never moved between the blocked attempt and the
/// passing one, and that main gained exactly one commit — the retry's, with
/// the retry's content.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn gate_blocks_the_merge_and_main_is_unchanged_until_the_retry_passes() {
    in_local(async {
        let script = r#"R=$(dirname "$(git rev-parse --git-common-dir)")
echo "$(git -C "$R" rev-parse main)" >> "$R/main-at-start.txt"
if [ ! -f "$R/second.flag" ]; then
  touch "$R/second.flag"
  echo bad > out.txt
else
  echo good > out.txt
fi"#;
        // The check fails only on the invocation whose pre-increment count is
        // 1 — the first attempt's gate.
        let check_cmd = counter_check(r#"[ "$N" -eq 1 ]"#, "exit 1");
        let Some(env) = env_or_skip("gate-blocks", script).await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-gate-blocks";
        let mut claim = env.claim(card, &["out.txt"], &check_cmd);
        claim.check_kind = "regression".into(); // Keep the counter's first run in the attempt.
        let id = format!("e2e-gate-blocks-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("gate_blocks", &id, &handle).await;
        // 1. The card still finishes as a merge — the blocked attempt cost one
        //    retry, not the card.
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(out.detail, "merged", "{out:?}");
        // 2. Exactly one line per attempt, and every line is the base: the
        //    blocked attempt left main where it found it.
        let starts = std::fs::read_to_string(env.repo.join("main-at-start.txt"))
            .expect("the script recorded main's ref at the start of every attempt");
        let starts: Vec<&str> = starts.lines().collect();
        assert_eq!(starts.len(), 2, "one line per attempt: {starts:?}");
        assert!(
            starts.iter().all(|s| *s == env.base_sha),
            "main did not move between the blocked attempt and the retry: {starts:?}"
        );
        // 3. The blocked attempt is judged `check_fail` and the final verdict
        //    the keeper holds is the retry's `ok`. The rows for attempt 1 are
        //    not pinned to one shape (a merge-time correction appends a second
        //    row instead of replacing the first), so only the presence of the
        //    failure is asserted. The gate's verdict is the one failure
        //    attempt 1 ever earns: exactly one `check_fail` row for n=1,
        //    judged before the final `ok`.
        let reports = env.keeper.attempts();
        let first: Vec<ExitClass> = reports
            .iter()
            .filter(|r| r.n == 1)
            .map(|r| r.exit_class)
            .collect();
        assert!(
            first.contains(&ExitClass::CheckFail),
            "attempt 1 is judged check_fail: {first:?}"
        );
        assert_eq!(
            reports.last().map(|r| r.exit_class),
            Some(ExitClass::Ok),
            "the final report is the retry that passed: {reports:?}"
        );
        assert_eq!(
            reports
                .iter()
                .filter(|r| r.n == 1 && r.exit_class == ExitClass::CheckFail)
                .count(),
            1,
            "the gate's verdict is reported exactly once for attempt 1: {reports:?}"
        );
        let fail_pos = reports
            .iter()
            .position(|r| r.exit_class == ExitClass::CheckFail)
            .expect("the gate's block is reported as a check failure");
        assert_eq!(
            reports[fail_pos].n, 1,
            "the blocked attempt is attempt 1: {reports:?}"
        );
        assert!(
            fail_pos < reports.len() - 1,
            "the gate's block is judged before the final ok: {reports:?}"
        );
        // 4. The retry moved main, and its tree is what main now holds.
        assert_ne!(
            git(&env.repo, &["rev-parse", "main"]),
            env.base_sha,
            "the retry must move main"
        );
        assert_eq!(
            git(&env.repo, &["show", "main:out.txt"]),
            "good",
            "main holds the retry's content, never the blocked attempt's"
        );
        // 5. The blocked attempt's content never landed: main is exactly the
        //    base plus the retry's one commit.
        let log = git(&env.repo, &["log", "--format=%H", "main"]);
        let shas: Vec<&str> = log.lines().collect();
        assert_eq!(shas.len(), 2, "main is base plus one commit: {shas:?}");
        assert_eq!(shas[1], env.base_sha, "the base is main's parent: {shas:?}");
        // 6. The gate is really in the loop, once per attempt: the blocked
        //    attempt's gate and the retry's gate are both in the history.
        let bytes = handle
            .fetch_history(WorkflowFetchHistoryOptions::default())
            .to_json()
            .await
            .expect("fetch history json");
        let raw = String::from_utf8_lossy(&bytes);
        assert!(
            raw.matches("CardActivities::gate").count() >= 2,
            "the gate must run once per attempt: {} occurrences",
            raw.matches("CardActivities::gate").count()
        );
        // 7. The counter saw exactly those invocations: attempt 1's check,
        //    its gate, the retry's check, its gate. Fewer means the command
        //    never reached the gate.
        let runs: u32 = std::fs::read_to_string(env.repo.join("chk.n"))
            .expect("the check counted its invocations in the shared repo")
            .trim()
            .parse()
            .expect("the counter is a number");
        assert!(
            runs >= 4,
            "the check ran in both attempts and both gates: {runs}"
        );
        env.stop();
    })
    .await;
}

/// A check that leaks a token when the GATE runs it must leave that token
/// nowhere durable: not in any keeper record (attempt reports with their
/// tails, alarms, park packets) and not in the workflow history, which stores
/// the claim and the attempt results verbatim. The attempt's own run passes
/// (see `counter_check`), so the leak is the gate's tail — and a gate tail is
/// redacted before it ever leaves the activity, so every stored form carries
/// `[REDACTED]` where the token was.
///
/// The fake token reaches the check as two shell arguments rather than one
/// literal: the claim — command included — is stored verbatim in the history's
/// start input, so a whole token in the command would defeat the absence
/// assertion by construction. Only the check's OUTPUT ever holds the whole
/// token, which is exactly the surface redaction must scrub.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_secret_in_a_failing_gate_never_reaches_the_keeper_or_history() {
    in_local(async {
        // Fake value, split mid-token so no whole token appears in the command
        // (and thus in the claim the history stores).
        let leak_marker = "abcd1234secretvalue9999";
        // The failing invocation is the gate's — odd counts (see
        // `counter_check`) — every time, so the gate is the failing surface
        // the budget eventually parks or quarantines the card on.
        let on_fail = format!(
            "printf 'API_TOKEN=%s%s\\n' {} {}; exit 1",
            &leak_marker[..8],
            &leak_marker[8..]
        );
        let check_cmd = counter_check(r#"[ $((N % 2)) -eq 1 ]"#, &on_fail);
        let Some(env) = env_or_skip("gate-secret", "echo ok > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-gate-secret";
        let mut claim = env.claim(card, &["out.txt"], &check_cmd);
        claim.check_kind = "regression".into(); // Keep the counter's first run in the attempt.
        let id = format!("e2e-gate-secret-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("gate_secret", &id, &handle).await;
        let parked = out.outcome == "parked" && out.detail == "parked";
        let quarantined = out.outcome == "failed" && out.detail == "quarantined";
        assert!(parked || quarantined, "unexpected outcome {out:?}");

        // No keeper record holds the token: the attempt reports (with their
        // tails), the alarms and the park packets are every surface the worker
        // hands the keeper.
        for (what, dump) in [
            ("attempt reports", format!("{:?}", env.keeper.attempts())),
            ("alarms", format!("{:?}", env.keeper.alarms())),
            ("park packets", format!("{:?}", env.keeper.parked())),
        ] {
            assert!(
                !dump.contains(leak_marker),
                "the token reached the keeper {what}: {dump}"
            );
        }

        // The history must not hold the token either — searched raw AND inside
        // its base64 payloads, because the strings that matter (the claim, the
        // attempt result's tail) live encoded there, invisible to a raw search.
        let bytes = handle
            .fetch_history(WorkflowFetchHistoryOptions::default())
            .to_json()
            .await
            .expect("fetch history json");
        let raw = String::from_utf8_lossy(&bytes);
        assert!(
            !raw.contains(leak_marker),
            "the token reached the workflow history"
        );
        let value: serde_json::Value = serde_json::from_slice(&bytes).expect("history is json");
        let decoded = decoded_payloads(&value).join("\n");
        assert!(
            !decoded.contains(leak_marker),
            "the token reached a history payload: {decoded}"
        );

        // The gate really ran — the attempt's check passed, so the leak is
        // the gate's invocation — and its tail was redacted before it left
        // the activity: the marker must be on record in a keeper record or a
        // history payload, right where the assertions above forbid the token
        // itself.
        assert!(
            raw.matches("CardActivities::gate").count() >= 1,
            "the gate must run after the attempt's check passed: {} occurrences",
            raw.matches("CardActivities::gate").count()
        );
        assert!(
            [
                ("attempt reports", format!("{:?}", env.keeper.attempts())),
                ("alarms", format!("{:?}", env.keeper.alarms())),
                ("park packets", format!("{:?}", env.keeper.parked())),
                ("history payloads", decoded),
            ]
            .into_iter()
            .any(|(_, d)| d.contains(REDACTED)),
            "the gate's redacted tail must reach a keeper record or the history"
        );
        env.stop();
    })
    .await;
}

/// A passing base check parks without retrying or invoking the writer.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn red_first_parks_without_retry_or_harness() {
    in_local(async {
        let Some(env) = env_or_skip("red-first", "echo ran > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let claim = env.claim("e2e-red-first", &["out.txt"], "true");
        let id = format!("e2e-red-first-{}", unique());
        let handle = env.start(&id, claim).await;
        let out = finish("red_first", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(out.detail, "parked", "{out:?}");
        assert_eq!(out.attempts, 1);
        let reports = env.keeper.attempts();
        assert_eq!(reports.len(), 1);
        assert_eq!(reports[0].exit_class, ExitClass::ContractDefect);
        assert!(reports[0].check_tail.starts_with("red-first:"));
        let packets = env.keeper.parked();
        assert_eq!(packets.len(), 1);
        assert_eq!(packets[0].exit_class, ExitClass::ContractDefect);
        assert!(packets[0].check_tail.starts_with("red-first:"));
        assert!(env.keeper.completed().is_empty());
        assert!(!env.repo.join("out.txt").exists());
        env.stop();
    })
    .await;
}

/// A leg naming a harness this worker never registered is the plan's fault:
/// the card goes back to the planner instead of crashing or retrying.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn unknown_harness_goes_to_planner() {
    in_local(async {
        let Some(env) = env_or_skip("unknown-harness", "echo ok > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-unknown-harness";
        let claim = env.claim(card, &["out.txt"], "test -f out.txt");
        let id = format!("e2e-unknown-harness-{}", unique());
        let legs = vec![LegSpec {
            id: "ghost-leg".to_string(),
            tier: Tier::Free,
            model: "some/model".to_string(),
            harness: "no-such-harness".to_string(),
        }];
        let handle = env.start_with_legs(&id, claim, legs).await;

        let out = finish("unknown_harness", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(out.detail, "planner", "{out:?}");
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

        let out = finish("contract_defect", &id, &handle).await;
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

        let out = finish("check_fails", &id, &handle).await;
        let parked = out.outcome == "parked" && out.detail == "parked";
        let quarantined = out.outcome == "failed" && out.detail == "quarantined";
        assert!(parked || quarantined, "unexpected outcome {out:?}");
        assert!(
            !env.keeper.alarms().is_empty() || !env.keeper.parked().is_empty(),
            "a stuck card raises an alarm or a park packet"
        );
        // A parked card: the park packet and its alarm both carry the card's
        // final attempt, with workflow id and run id.
        assert_execs_name_the_attempts(&env, out.attempts);
        env.stop();
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn cancel_signal_stops_a_running_attempt() {
    in_local(async {
        // The attempt runs until killed, far past the deadline below, so a pass
        // can only mean the cancel signal killed it rather than the script
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
        handle
            .signal(
                Card::cancel,
                "operator".to_string(),
                WorkflowSignalOptions::default(),
            )
            .await
            .expect("send cancel signal");

        let out = tokio::time::timeout(
            CANCEL_BUDGET,
            handle.get_result(WorkflowGetResultOptions::default()),
        )
        .await
        .expect("cancel takes effect within the cancel budget")
        .expect("cancelled card returns a result");
        record("cancel", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(out.detail, "cancelled", "{out:?}");
        // Parked is not enough: the shell child must also be dead, which is what
        // actually lets the test binary exit promptly.
        assert_child_gone(&marker).await;
        env.stop();
    })
    .await;
}

/// A marker path the harness writes its own pid to the moment it starts.
fn started_marker() -> PathBuf {
    std::env::temp_dir().join(format!("pg-e2e-started-{}", unique()))
}

fn started_script(marker: &Path) -> String {
    // Write the shell's own pid into the marker, then run until killed: the pid
    // lets `assert_child_gone` prove the cancel killed this exact child, and the
    // endless chatty loop both outlives every cancel budget and keeps the stall
    // watchdog from firing.
    format!(
        "echo $$ > '{}'; while :; do echo alive; sleep 1; done",
        marker.display()
    )
}

/// Poll until the harness has written a usable pid into `marker`.
///
/// The shell creates the file before the pid bytes land, so a bare existence
/// check could read a half-written marker; require a non-empty integer instead.
async fn wait_for_marker(marker: &Path) {
    let deadline = std::time::Instant::now() + Duration::from_secs(60);
    loop {
        let ready = std::fs::read_to_string(marker)
            .ok()
            .is_some_and(|s| s.trim().parse::<u32>().is_ok());
        if ready {
            return;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "attempt never started (no marker)"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Prove the harness child is gone before the test ends.
///
/// The cancel tests finish as soon as the workflow returns, but the supervised
/// `sh` child may still be alive; if it is, `Env::stop` aborts the worker and
/// the test binary blocks at exit until the child's own stall/timeout, roughly
/// doubling the run. Waiting on the child's pid from the marker removes that
/// wait and strengthens the cancel proof: a parked card plus a dead child show
/// the cancel reached the running attempt. `/proc/<pid>` is Linux-only, like
/// the rest of this harness.
async fn assert_child_gone(marker: &Path) {
    let pid: u32 = std::fs::read_to_string(marker)
        .expect("marker holds the harness pid")
        .trim()
        .parse()
        .expect("marker holds the harness pid");
    let deadline = std::time::Instant::now() + Duration::from_secs(120);
    while Path::new(&format!("/proc/{pid}")).exists() {
        if std::time::Instant::now() >= deadline {
            // Read the state back from procfs: a `State: Z` line means the child
            // was killed but never reaped, which blames the supervisor rather
            // than the cancel path. Missing or unreadable status stays "unknown"
            // so the panic still names what it could see.
            let status = std::fs::read_to_string(format!("/proc/{pid}/status")).ok();
            let field = |key: &str| -> String {
                status
                    .as_deref()
                    .and_then(|s| s.lines().find_map(|l| l.strip_prefix(key)))
                    .map(|v| v.trim().to_string())
                    .unwrap_or_else(|| "unknown".to_string())
            };
            panic!(
                "cancel did not kill the harness child: pid {pid} state {} ppid {}",
                field("State:"),
                field("PPid:")
            );
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Send `cancel` and require a `cancelled` detail inside the budget.
///
/// `marker` is the harness start marker: if the signal landed before the harness
/// ever started there is no child and no marker, so only assert when it exists.
async fn cancel_and_expect(env: &Env, handle: &Handle, marker: &Path) {
    handle
        .signal(
            Card::cancel,
            "operator".to_string(),
            WorkflowSignalOptions::default(),
        )
        .await
        .expect("send cancel signal");
    let out = tokio::time::timeout(
        CANCEL_BUDGET,
        handle.get_result(WorkflowGetResultOptions::default()),
    )
    .await
    .expect("cancel takes effect within the cancel budget")
    .expect("cancelled card returns a result");
    assert_eq!(out.outcome, "parked", "{out:?}");
    assert_eq!(out.detail, "cancelled", "{out:?}");
    if marker.exists() {
        assert_child_gone(marker).await;
    }
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
        cancel_and_expect(&env, &handle, &marker).await;
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
            CANCEL_BUDGET,
            handle.get_result(WorkflowGetResultOptions::default()),
        )
        .await
        .expect("cancel takes effect within the cancel budget")
        .expect("cancelled card returns a result");
        assert_eq!(out.outcome, "parked", "{out:?}");
        assert_eq!(out.detail, "cancelled", "{out:?}");
        assert!(!marker.exists(), "no attempt may start after the cancel");
        env.stop();
    })
    .await;
}

async fn finish_phase(name: &str, handle: &WorkflowHandle<Client, Phase>) -> PhaseOutput {
    let out = tokio::time::timeout(
        RESULT_TIMEOUT,
        handle.get_result(WorkflowGetResultOptions::default()),
    )
    .await
    .expect("phase result within 120s")
    .expect("phase workflow result");
    record_phase(name, handle).await;
    out
}

async fn record_phase(name: &str, handle: &WorkflowHandle<Client, Phase>) {
    let Some(dir) = std::env::var_os("PG_RECORD_DIR").map(PathBuf::from) else {
        return;
    };
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
}

fn plan_json(key: &str, cards: &[(&str, bool)]) -> pg_worker::phase::PhasePlan {
    let cards: Vec<_> = cards
        .iter()
        .map(|&(key, deferred)| {
            serde_json::json!({
                "key": key,
                "ready": true,
                "deferred": deferred,
                "write_set": [format!("{key}.txt")],
                "route": {
                    "window_class": "128k",
                    "harness": "opencode",
                    "legs": ["leg-1"],
                },
            })
        })
        .collect();
    serde_json::from_value(serde_json::json!({
        "phase": key,
        "ready": true,
        "blocked": [],
        "cards": cards,
    }))
    .expect("phase plan json")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn phase_runs_two_cards_into_the_phase_branch() {
    in_local(async {
        let Some(env) = env_or_skip("phase-two", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let k = |s: &str| format!("e2e-{s}-{}", unique());
        let a = k("phase-a");
        let b = k("phase-b");
        let phase_key = format!("phase-{}", unique());
        env.keeper.push_plan(plan_json(
            &phase_key,
            &[(a.as_str(), false), (b.as_str(), false)],
        ));

        let file_a = format!("{a}.txt");
        let mut claim_a = env.claim(&a, &[file_a.as_str()], &format!("test -f {file_a}"));
        claim_a.prompt = file_a;
        env.keeper.push_card_claim(claim_a);

        let file_b = format!("{b}.txt");
        let mut claim_b = env.claim(&b, &[file_b.as_str()], &format!("test -f {file_b}"));
        claim_b.prompt = file_b;
        env.keeper.push_card_claim(claim_b);

        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "merged".to_string(),
            pr: Some(pg_worker::phase::PhasePr {
                number: 7,
                url: "u".to_string(),
            }),
            detail: String::new(),
        });

        let id = format!("e2e-phase-two-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 2)).await;
        let out = finish_phase("phase_two_cards", &handle).await;

        assert_eq!(out.outcome, "done", "{out:?}");
        let mut merged = out.merged.clone();
        merged.sort();
        let mut expected = vec![a.clone(), b.clone()];
        expected.sort();
        assert_eq!(merged, expected, "{out:?}");
        assert!(out.not_merged.is_empty(), "{out:?}");
        assert_eq!(out.waves, 1, "{out:?}");
        assert_eq!(out.branch, format!("phase/{}", phase_key));
        assert_eq!(
            git(&env.repo, &["rev-parse", "refs/heads/main"]),
            env.base_sha
        );

        let tree = git(&env.repo, &["ls-tree", "-r", "--name-only", &out.branch]);
        assert!(tree.contains(format!("{a}.txt").as_str()), "{tree}");
        assert!(tree.contains(format!("{b}.txt").as_str()), "{tree}");
        assert!(tree.contains("seed.txt"), "{tree}");

        let completed = env.keeper.completed();
        assert!(completed.iter().any(|(c, _)| c == &a), "{completed:?}");
        assert!(completed.iter().any(|(c, _)| c == &b), "{completed:?}");
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn phase_blocked_plan_creates_no_branch() {
    in_local(async {
        let Some(env) = env_or_skip("phase-blocked", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let phase_key = format!("phase-{}", unique());
        let plan: pg_worker::phase::PhasePlan = serde_json::from_value(serde_json::json!({
            "phase": phase_key.clone(),
            "ready": false,
            "blocked": ["no opencode leg"],
            "cards": [],
        }))
        .expect("blocked phase plan");
        env.keeper.push_plan(plan);

        let id = format!("e2e-phase-blocked-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 2)).await;
        let out = finish_phase("phase_blocked", &handle).await;

        assert_eq!(out.outcome, "blocked", "{out:?}");
        assert_eq!(out.blocked, vec!["no opencode leg".to_string()], "{out:?}");
        assert!(out.merged.is_empty(), "{out:?}");
        assert!(out.not_merged.is_empty(), "{out:?}");
        assert_eq!(out.waves, 0, "{out:?}");
        assert!(git(&env.repo, &["branch", "--list", "phase/*"]).is_empty());
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn phase_defers_an_overlapping_card_to_a_later_wave() {
    in_local(async {
        let Some(env) = env_or_skip("phase-defer", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let k = |s: &str| format!("e2e-{s}-{}", unique());
        let a = k("defer-a");
        let b = k("defer-b");
        let phase_key = format!("phase-{}", unique());
        env.keeper.push_plan(plan_json(
            &phase_key,
            &[(a.as_str(), false), (b.as_str(), true)],
        ));
        env.keeper.push_plan(plan_json(
            &phase_key,
            &[(a.as_str(), false), (b.as_str(), false)],
        ));

        let file_a = format!("{a}.txt");
        let mut claim_a = env.claim(&a, &[file_a.as_str()], &format!("test -f {file_a}"));
        claim_a.prompt = file_a;
        env.keeper.push_card_claim(claim_a);

        let file_b = format!("{b}.txt");
        let mut claim_b = env.claim(&b, &[file_b.as_str()], &format!("test -f {file_b}"));
        claim_b.prompt = file_b;
        env.keeper.push_card_claim(claim_b);

        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "merged".to_string(),
            pr: Some(pg_worker::phase::PhasePr {
                number: 7,
                url: "u".to_string(),
            }),
            detail: String::new(),
        });
        let id = format!("e2e-phase-defer-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 2)).await;
        let out = finish_phase("phase_defer", &handle).await;

        assert_eq!(out.waves, 2, "{out:?}");
        let mut merged = out.merged.clone();
        merged.sort();
        let mut expected = vec![a.clone(), b.clone()];
        expected.sort();
        assert_eq!(merged, expected, "{out:?}");
        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(env.keeper.finish_calls().len(), 1);
        assert!(env.keeper.plan_calls().len() >= 2);
    })
    .await;
}

#[tokio::test]
async fn phase_wave_cap_stops_before_the_next_wave() {
    in_local(async {
        let Some(env) = env_or_skip("phase-wave-cap", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let k = |s: &str| format!("e2e-{s}-{}", unique());
        let a = k("wave-cap-a");
        let b = k("wave-cap-b");
        let phase_key = format!("phase-{}", unique());
        env.keeper.push_plan(plan_json(
            &phase_key,
            &[(a.as_str(), false), (b.as_str(), true)],
        ));
        env.keeper.push_plan(plan_json(
            &phase_key,
            &[(a.as_str(), false), (b.as_str(), false)],
        ));

        let file_a = format!("{a}.txt");
        let mut claim_a = env.claim(&a, &[file_a.as_str()], &format!("test -f {file_a}"));
        claim_a.prompt = file_a;
        env.keeper.push_card_claim(claim_a);

        let id = format!("e2e-phase-wave-cap-{}", unique());
        let mut input = env.phase_input(&phase_key, 2);
        input.max_waves = Some(1);
        let handle = env.start_phase(&id, input).await;
        let out = finish_phase("phase_wave_cap", &handle).await;

        assert_eq!(out.outcome, "partial", "{out:?}");
        assert_eq!(out.waves, 1, "{out:?}");
        assert_eq!(out.merged, vec![a.clone()], "{out:?}");
        assert_eq!(out.blocked, vec!["wave cap reached".to_string()], "{out:?}");
        assert!(
            env.keeper.finish_calls().is_empty(),
            "{:?}",
            env.keeper.finish_calls()
        );
    })
    .await;
}

#[tokio::test]
async fn phase_unstartable_card_is_reported_not_done() {
    in_local(async {
        let Some(env) = env_or_skip("phase-unstartable", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = format!("e2e-unstartable-{}", unique());
        let phase_key = format!("phase-{}", unique());
        env.keeper.push_plan(
            serde_json::from_value::<pg_worker::phase::PhasePlan>(serde_json::json!({
                "phase": phase_key.clone(),
                "ready": true,
                "blocked": [],
                "cards": [{
                    "key": card,
                    "ready": false,
                    "deferred": false,
                    "write_set": [],
                    "route": {
                        "window_class": "128k",
                        "harness": "opencode",
                        "legs": ["leg-1"]
                    }
                }]
            }))
            .expect("phase plan"),
        );

        let id = format!("e2e-phase-unstartable-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 2)).await;
        let out = finish_phase("phase_unstartable", &handle).await;

        assert_eq!(out.outcome, "blocked", "{out:?}");
        assert!(out.merged.is_empty(), "{out:?}");
        assert!(
            out.blocked
                .first()
                .is_some_and(|s| s.starts_with("cards not startable")),
            "{out:?}"
        );
        assert_eq!(out.waves, 0, "{out:?}");
        let branches = git(&env.repo, &["branch", "--list", "phase/*"]);
        assert!(branches.trim().is_empty(), "{branches:?}");
        assert!(
            env.keeper.finish_calls().is_empty(),
            "{:?}",
            env.keeper.finish_calls()
        );
    })
    .await;
}

#[tokio::test]
async fn phase_finish_waits_for_ci_then_merges() {
    in_local(async {
        let Some(env) = env_or_skip("phase-finish-ci", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = format!("e2e-finish-ci-{}", unique());
        let phase_key = format!("phase-{}", unique());
        env.keeper
            .push_plan(plan_json(&phase_key, &[(card.as_str(), false)]));
        let file = format!("{card}.txt");
        let mut claim = env.claim(&card, &[file.as_str()], &format!("test -f {file}"));
        claim.prompt = file;
        env.keeper.push_card_claim(claim);
        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "waiting_ci".to_string(),
            pr: None,
            detail: String::new(),
        });
        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "merged".to_string(),
            pr: Some(pg_worker::phase::PhasePr {
                number: 7,
                url: "u".to_string(),
            }),
            detail: String::new(),
        });

        let id = format!("e2e-phase-finish-ci-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 1)).await;
        let out = finish_phase("phase_finish_ci", &handle).await;

        assert_eq!(out.outcome, "done", "{out:?}");
        assert_eq!(
            out.finish.as_ref().map(|f| f.state.as_str()),
            Some("merged"),
            "{out:?}"
        );
        assert_eq!(env.keeper.finish_calls().len(), 2);
    })
    .await;
}

#[tokio::test]
async fn phase_finish_refused_is_partial() {
    in_local(async {
        let Some(env) = env_or_skip("phase-finish-refused", "echo ok > \"$PG_PROMPT\"").await
        else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = format!("e2e-finish-refused-{}", unique());
        let phase_key = format!("phase-{}", unique());
        env.keeper
            .push_plan(plan_json(&phase_key, &[(card.as_str(), false)]));
        let file = format!("{card}.txt");
        let mut claim = env.claim(&card, &[file.as_str()], &format!("test -f {file}"));
        claim.prompt = file;
        env.keeper.push_card_claim(claim);
        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "refused".to_string(),
            pr: None,
            detail: "ci red".to_string(),
        });

        let id = format!("e2e-phase-finish-refused-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 1)).await;
        let out = finish_phase("phase_finish_refused", &handle).await;

        assert_eq!(out.outcome, "partial", "{out:?}");
        assert!(out.blocked.iter().any(|s| s == "ci red"), "{out:?}");
    })
    .await;
}

#[tokio::test]
async fn phase_finish_unknown_state_is_partial() {
    in_local(async {
        let Some(env) = env_or_skip("phase-finish-unknown", "echo ok > \"$PG_PROMPT\"").await
        else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = format!("e2e-finish-unknown-{}", unique());
        let phase_key = format!("phase-{}", unique());
        env.keeper
            .push_plan(plan_json(&phase_key, &[(card.as_str(), false)]));
        let file = format!("{card}.txt");
        let mut claim = env.claim(&card, &[file.as_str()], &format!("test -f {file}"));
        claim.prompt = file;
        env.keeper.push_card_claim(claim);
        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "conflict".to_string(),
            pr: None,
            detail: String::new(),
        });

        let id = format!("e2e-phase-finish-unknown-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 1)).await;
        let out = finish_phase("phase_finish_unknown", &handle).await;

        assert_eq!(out.outcome, "partial", "{out:?}");
        assert!(
            out.blocked.iter().any(|s| s.contains("unknown state")),
            "{out:?}"
        );
        assert_eq!(env.keeper.finish_calls().len(), 1, "{out:?}");
    })
    .await;
}

#[tokio::test]
async fn phase_finish_never_green_is_partial() {
    in_local(async {
        let Some(env) = env_or_skip("phase-finish-never-green", "echo ok > \"$PG_PROMPT\"").await
        else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = format!("e2e-finish-never-green-{}", unique());
        let phase_key = format!("phase-{}", unique());
        env.keeper
            .push_plan(plan_json(&phase_key, &[(card.as_str(), false)]));
        let file = format!("{card}.txt");
        let mut claim = env.claim(&card, &[file.as_str()], &format!("test -f {file}"));
        claim.prompt = file;
        env.keeper.push_card_claim(claim);
        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "waiting_ci".to_string(),
            pr: None,
            detail: String::new(),
        });

        let id = format!("e2e-phase-finish-never-green-{}", unique());
        let mut input = env.phase_input(&phase_key, 1);
        input.finish_max_polls = Some(2);
        input.finish_poll_s = Some(1);
        let handle = env.start_phase(&id, input).await;
        let out = finish_phase("phase_finish_never_green", &handle).await;

        assert_eq!(out.outcome, "partial", "{out:?}");
        assert!(out.blocked.iter().any(|s| s.contains("ci-gate")), "{out:?}");
        assert_eq!(env.keeper.finish_calls().len(), 2);
    })
    .await;
}

#[tokio::test]
async fn phase_card_without_claim_is_not_merged() {
    in_local(async {
        let Some(env) = env_or_skip("phase-no-claim", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = format!("e2e-no-claim-{}", unique());
        let phase_key = format!("phase-{}", unique());
        env.keeper
            .push_plan(plan_json(&phase_key, &[(card.as_str(), false)]));

        let id = format!("e2e-phase-no-claim-{}", unique());
        let handle = env.start_phase(&id, env.phase_input(&phase_key, 2)).await;
        let out = finish_phase("phase_no_claim", &handle).await;

        assert_eq!(out.outcome, "partial", "{out:?}");
        assert_eq!(out.not_merged, vec![card.clone()], "{out:?}");
        assert!(out.merged.is_empty(), "{out:?}");
    })
    .await;
}

#[tokio::test]
async fn phase_rerun_reuses_a_closed_card_child_id() {
    in_local(async {
        let Some(env) = env_or_skip("phase-rerun", "echo ok > \"$PG_PROMPT\"").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let c = format!("e2e-rerun-{}", unique());
        let phase_key = format!("phase-{}", unique());

        env.keeper
            .push_plan(plan_json(&phase_key, &[(c.as_str(), false)]));
        let mut claim1 = env.claim(&c, &["other.txt"], "test -f never.txt");
        claim1.prompt = "other.txt".to_string();
        env.keeper.push_card_claim(claim1);

        let id1 = format!("e2e-phase-rerun-{}-{}", 1, unique());
        let handle1 = env.start_phase(&id1, env.phase_input(&phase_key, 1)).await;
        let out1 = finish_phase("phase_rerun_1", &handle1).await;
        assert_eq!(out1.not_merged, vec![c.clone()], "{out1:?}");
        assert!(out1.merged.is_empty(), "{out1:?}");

        env.keeper
            .push_plan(plan_json(&phase_key, &[(c.as_str(), false)]));
        let file = format!("{c}.txt");
        let mut claim2 = env.claim(&c, &[file.as_str()], &format!("test -f {file}"));
        claim2.prompt = file.clone();
        env.keeper.push_card_claim(claim2);

        env.keeper.push_finish(pg_worker::phase::PhaseFinish {
            state: "merged".to_string(),
            pr: Some(pg_worker::phase::PhasePr {
                number: 7,
                url: "u".to_string(),
            }),
            detail: String::new(),
        });
        let id2 = format!("e2e-phase-rerun-{}-{}", 2, unique());
        let handle2 = env.start_phase(&id2, env.phase_input(&phase_key, 1)).await;
        let out2 = finish_phase("phase_rerun_2", &handle2).await;

        assert_eq!(out2.outcome, "done", "{out2:?}");
        assert_eq!(out2.merged, vec![c.clone()], "{out2:?}");
        assert!(out2.not_merged.is_empty(), "{out2:?}");
        let completed = env.keeper.completed();
        assert!(
            completed.iter().any(|(card, _)| card == &c),
            "{completed:?}"
        );
        env.stop();
    })
    .await;
}

/// R2-1 / R2-3 / I1: a fresh run that parks schedules `observe`, records the
/// `park-observe-v1` marker in workflow history, and presents the observed
/// state in `park`, never the live-set fallback.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn fresh_run_records_observe_patch_and_parks_with_observation() {
    in_local(async {
        let Some(env) = env_or_skip("observe-park", "echo ran > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-observe-park";
        // The keeper holds state `waiting` (e.g. usage_limit/outage).
        env.keeper.set_card_state(Some("waiting"));
        let claim = env.claim(card, &["out.txt"], "true");
        let id = format!("e2e-observe-park-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("observe_park", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");

        // Invariant I1: observe was scheduled before park.
        assert_eq!(
            env.keeper.card_state_calls(),
            vec![card.to_string()],
            "observe activity must query card_state before park"
        );

        // Invariant I1: park carries the observed state ["waiting"], NOT the live-set fallback.
        let execs = env.keeper.execs();
        let park_exec = execs
            .iter()
            .find(|e| e.expected_state.is_some())
            .expect("park must carry expected_state");
        assert_eq!(
            park_exec.expected_state,
            Some(vec!["waiting".to_string()]),
            "park must carry recorded observation, not live-set fallback"
        );
        assert_eq!(env.keeper.parked().len(), 1, "park must have landed");

        // Invariant I1: OBSERVE_PATCH marker was recorded in workflow history.
        let bytes = handle
            .fetch_history(WorkflowFetchHistoryOptions::default())
            .to_json()
            .await
            .expect("fetch history json");
        let history_val: serde_json::Value =
            serde_json::from_slice(&bytes).expect("history is valid json");
        let decoded = decoded_payloads(&history_val).join("\n");
        assert!(
            decoded.contains("park-observe-v1"),
            "workflow history must record park-observe-v1 marker: {decoded}"
        );

        env.stop();
    })
    .await;
}

/// R2-2 / I3: a transient observe failure (500/timeout/None) makes the first park
/// send the live-set fallback, which the keeper refuses with 409 exec_stale_state
/// on a `waiting` card. The workflow must re-observe and land the cap-park.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn cap_park_on_waiting_card_lands_after_one_failed_observe() {
    in_local(async {
        let Some(env) = env_or_skip("failed-observe-park", "echo ran > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-failed-observe-park";
        // Card is waiting; simulate one transient read failure (returns None).
        env.keeper.set_card_state(Some("waiting"));
        env.keeper.set_card_state_fail_times(1);

        let claim = env.claim(card, &["out.txt"], "true");
        let id = format!("e2e-failed-observe-park-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("failed_observe_park", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");

        // Invariant I3: the workflow re-observed after fallback refusal and landed the park.
        assert_eq!(
            env.keeper.card_state_calls().len(),
            2,
            "observe activity must have been re-run after fallback refusal"
        );
        assert_eq!(
            env.keeper.parked().len(),
            1,
            "park must land within bounded retries"
        );

        env.stop();
    })
    .await;
}

/// D1b / I3a: a transient observe read failure is retried by the activity's
/// retry policy inside one scheduled `observe`, not degraded to the live-set
/// fallback: two `Err(500)` reads then the state, and the park carries the
/// observed state ["waiting"].
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn transient_observe_failure_is_retried_not_degraded() {
    in_local(async {
        let Some(env) = env_or_skip("retried-observe", "echo ran > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };
        let card = "e2e-retried-observe";
        // Card is waiting; the first two reads fail transiently (D1a Err(500)).
        env.keeper.set_card_state(Some("waiting"));
        env.keeper.set_card_state_fail_times(2);

        let claim = env.claim(card, &["out.txt"], "true");
        let id = format!("e2e-retried-observe-{}", unique());
        let handle = env.start(&id, claim).await;

        let out = finish("retried_observe", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");

        // I3a: one scheduled observe activity: the retries are server-side
        // attempts, so the workflow issued a single command and replay is safe.
        assert_eq!(
            scheduled_activities(&handle, "observe").await,
            1,
            "a transient read failure must be retried by the activity, not degraded"
        );
        // I3a: two failed reads, then the successful observation.
        assert_eq!(
            env.keeper.card_state_calls().len(),
            3,
            "two transient failures then the observed state"
        );
        // I3a: the park carries the observed state, never the live-set fallback.
        let execs = env.keeper.execs();
        let park_exec = execs
            .iter()
            .find(|e| e.expected_state.is_some())
            .expect("park must carry expected_state");
        assert_eq!(
            park_exec.expected_state,
            Some(vec!["waiting".to_string()]),
            "the retried read must land the observation, not degrade"
        );
        assert_eq!(env.keeper.parked().len(), 1, "the park must land");

        env.stop();
    })
    .await;
}

/// D2/D3/D4 (I3b): a park the keeper refuses 409 (`exec_stale_state`, the state
/// CAS missed) is the one failure a fresh observation can repair, so the
/// workflow re-observes and parks again -- at most twice. A refusal the
/// re-observe repairs lands the park on the first re-observe; a keeper that
/// refuses every park fails the workflow after exactly 2 re-observes, with the
/// last cause in the failure message.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn stale_state_refusal_reobserves_at_most_twice() {
    in_local(async {
        let Some(env) = env_or_skip("stale-park", "echo ran > out.txt").await else {
            println!("skipped: PG_TEMPORAL_ADDR unset");
            return;
        };

        // One refused park: the re-observe repairs it and the park lands.
        env.keeper.set_card_state(Some("waiting"));
        env.keeper.set_park_refuse_times(1);
        let claim = env.claim("e2e-stale-park", &["out.txt"], "true");
        let id = format!("e2e-stale-park-{}", unique());
        let handle = env.start(&id, claim).await;
        let out = finish("stale_park", &id, &handle).await;
        assert_eq!(out.outcome, "parked", "{out:?}");
        // I3b: lands on re-observe 1: the initial observe plus exactly one more.
        assert_eq!(scheduled_activities(&handle, "observe").await, 2);
        assert_eq!(
            scheduled_activities(&handle, "CardActivities::park").await,
            2
        );
        assert_eq!(env.keeper.parked().len(), 1);
        // I3b: the landing park presents the re-observed state, not a stale one.
        let park = env
            .keeper
            .execs()
            .into_iter()
            .rfind(|e| e.expected_state.is_some())
            .expect("park exec");
        assert_eq!(park.expected_state, Some(vec!["waiting".to_string()]));

        // A keeper that refuses every park: bounded at 2 re-observes, then fail.
        env.keeper.set_park_refuse_times(3);
        let claim = env.claim("e2e-stale-park-fail", &["out.txt"], "true");
        let id = format!("e2e-stale-park-fail-{}", unique());
        let handle = env.start(&id, claim).await;
        let err = tokio::time::timeout(
            RESULT_TIMEOUT,
            handle.get_result(WorkflowGetResultOptions::default()),
        )
        .await
        .expect("card result within 120s")
        .expect_err("a park that can never land fails the workflow");
        let temporalio_client::errors::WorkflowGetResultError::Failed(failure) = err else {
            panic!("expected a failed workflow, got: {err:?}");
        };
        let msg = &failure.failure().message;
        assert!(msg.contains("after 2 re-observes"), "{msg}");
        assert!(msg.contains("409"), "the last cause must ride in: {msg}");
        // I3b: initial observe + exactly 2 re-observes, and no park landed.
        assert_eq!(scheduled_activities(&handle, "observe").await, 3);
        assert_eq!(
            scheduled_activities(&handle, "CardActivities::park").await,
            3
        );
        // The stub still holds the first scenario's one landed park: none added.
        assert_eq!(env.keeper.parked().len(), 1, "no refused park may land");

        env.stop();
    })
    .await;
}
