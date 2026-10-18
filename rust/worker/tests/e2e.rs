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

#![cfg(unix)]

use pg_worker_core::exit_class::ExitClass;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use pg_worker::activities::{CardActivities, CardInput, CardOutput, Deps, TASK_QUEUE};
use pg_worker::attempt::LegSpec;
use pg_worker::harness::{Harness, HarnessRegistry, HarnessRun};
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
            claim,
            legs,
            workroot: self.workroot.to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
            question_wait_cap_s: self.question_cap_s.get(),
            max_question_rounds: self.max_rounds.get(),
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
                .any(|a| a.reason == "question unanswered"),
            "{:?}",
            env.keeper.alarms()
        );
        assert_eq!(env.keeper.parked().len(), 1);
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
        let claim = env.claim(card, &["seed.txt"], "test -f seed.txt");
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
