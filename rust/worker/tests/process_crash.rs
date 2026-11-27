//! Real deployment-routing process-death regression using one compiled binary.
//! This proves server pinning, not compatibility between independently built releases.
#![cfg(unix)]
use std::{
    os::unix::process::ExitStatusExt,
    path::Path,
    process::{Child, Command, Stdio},
    sync::Arc,
    time::Duration,
};
use temporalio_client::{
    Client, ClientOptions, Connection, ConnectionOptions, Url, WorkflowFetchHistoryOptions,
    WorkflowGetResultOptions, WorkflowStartOptions,
};
use temporalio_common::worker::{
    VersioningBehavior, WorkerDeploymentOptions, WorkerDeploymentVersion,
};
use temporalio_macros::{activities, workflow, workflow_methods};
use temporalio_sdk::{
    ActivityOptions, Runtime, SyncWorkflowContext, Worker, WorkerOptions, WorkflowContext,
    WorkflowResult,
    activities::{ActivityContext, ActivityError},
};

#[workflow]
#[derive(Default)]
struct CrashWorkflow {
    released: bool,
}
#[workflow_methods]
impl CrashWorkflow {
    #[run]
    async fn run(ctx: &mut WorkflowContext<Self>, mode: String) -> WorkflowResult<String> {
        if mode == "resumed" {
            return Ok("rolled".into());
        }
        if mode == "rollover-events" || mode == "rollover-waits" {
            let events = if mode == "rollover-events" {
                pg_worker::rollover::EVENT_CAP
            } else {
                0
            };
            let waits = if mode == "rollover-waits" {
                pg_worker::rollover::WAIT_CAP
            } else {
                0
            };
            if pg_worker::rollover::due(events, waits, false) {
                return ctx
                    .continue_as_new("resumed".to_string(), Default::default())
                    .map(|v| match v {});
            }
        }
        if mode == "retry-cap" {
            let mut opts =
                ActivityOptions::with_start_to_close_timeout(Duration::from_millis(20)).build();
            // Production binding includes 60 seconds of queue/retry allowance.
            pg_worker::rollover::bound(&mut opts, Duration::from_millis(20));
            let result = ctx.execute_activity(Probe::retry, (), opts).await;
            return Ok(if result.is_err() {
                "capped"
            } else {
                "uncapped"
            }
            .into());
        }
        if mode == "timer" {
            temporalio_sdk::workflows::select! {
                r = ctx.wait_condition(|s: &Self| s.released) => { r?; }
                _ = ctx.timer(Duration::from_secs(600)) => return Ok("timer fired before release".into()),
            }
        }
        let opts = ActivityOptions::with_start_to_close_timeout(Duration::from_secs(60)).build();
        Ok(ctx.execute_activity(Probe::attempt, (), opts).await?)
    }

    #[signal]
    fn release(&mut self, _: &mut SyncWorkflowContext<Self>, _: ()) {
        self.released = true;
    }
}
struct Probe;
#[activities]
impl Probe {
    #[activity]
    async fn retry(self: Arc<Self>, _: ActivityContext, _: ()) -> Result<String, ActivityError> {
        Err(std::io::Error::other("retry until schedule-to-close").into())
    }
    #[activity]
    async fn attempt(self: Arc<Self>, _: ActivityContext, _: ()) -> Result<String, ActivityError> {
        let ready = std::env::var_os("CRASH_READY").unwrap();
        if !ready.is_empty() {
            std::fs::write(ready, b"attempt").unwrap();
            std::future::pending::<()>().await;
        }
        Ok(std::env::var("CRASH_BUILD").unwrap())
    }
}
async fn client(addr: &str) -> Client {
    let conn = Connection::connect(ConnectionOptions::new(addr.parse::<Url>().unwrap()).build())
        .await
        .unwrap();
    Client::new(conn, ClientOptions::new("default").build()).unwrap()
}

#[test]
fn child_worker() {
    let Ok(addr) = std::env::var("CRASH_ADDR") else {
        return;
    };
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            tokio::task::LocalSet::new()
                .run_until(async {
                    let rt = Runtime::from_current_tokio(Default::default()).unwrap();
                    let opts = WorkerOptions::new(std::env::var("CRASH_QUEUE").unwrap())
                        .register_workflow::<CrashWorkflow>()
                        .unwrap()
                        .register_activities(Probe)
                        .deployment_options(
                            WorkerDeploymentOptions::new(
                                WorkerDeploymentVersion::builder()
                                    .deployment_name(std::env::var("CRASH_QUEUE").unwrap())
                                    .build_id(std::env::var("CRASH_BUILD").unwrap())
                                    .build(),
                            )
                            .use_worker_versioning(true)
                            .default_versioning_behavior(VersioningBehavior::Pinned)
                            .build(),
                        )
                        .build();
                    Worker::new(&rt, client(&addr).await, opts)
                        .unwrap()
                        .run()
                        .await
                        .unwrap();
                })
                .await;
        });
}

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn worker(addr: &str, queue: &str, build: &str, ready: Option<&Path>) -> Process {
    Process(
        Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "child_worker", "--nocapture"])
            .env("CRASH_ADDR", addr)
            .env("CRASH_QUEUE", queue)
            .env("CRASH_BUILD", build)
            .env("CRASH_READY", ready.unwrap_or(Path::new("")))
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    )
}

#[test]
fn fixtures_only_reports_crash_skip_without_temporal() {
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "--show-output"])
        .arg("sigkill_mid_attempt_and_mid_timer_stays_pinned")
        .env_clear()
        .env("PG_FAST_CHECK_FIXTURES_ONLY", "1")
        .env("TEMPORAL_CLI", "sentinel-temporal-cli")
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let stdout = String::from_utf8(output.stdout).unwrap();
    let marker = "PG_FAST_CHECK_SKIP sigkill_mid_attempt_and_mid_timer_stays_pinned \
                  reason=requires_temporal_deployment_fixture";
    assert_eq!(
        stdout.lines().filter(|line| *line == marker).count(),
        1,
        "{stdout}"
    );
}

#[test]
fn sigkill_mid_attempt_and_mid_timer_stays_pinned() {
    if std::env::var("PG_FAST_CHECK_FIXTURES_ONLY").as_deref() == Ok("1") {
        println!(
            "PG_FAST_CHECK_SKIP sigkill_mid_attempt_and_mid_timer_stays_pinned reason=requires_temporal_deployment_fixture"
        );
        return;
    }
    let cli = std::env::var("TEMPORAL_CLI").expect("run the acceptance suite through ci-temporal");
    let addr = std::env::var("PG_TEMPORAL_ADDR").expect("ci-temporal supplies an isolated address");
    let address = addr.strip_prefix("http://").unwrap();
    let temp = tempfile::tempdir().unwrap();
    let name = |mode: &str| format!("pg-process-crash-{mode}");
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let c = client(&addr).await;
            for mode in ["attempt", "timer"] {
                let id = name(mode);
                let ready = temp.path().join(mode);
                let mut old = worker(&addr, &id, "old", (mode == "attempt").then_some(&ready));
                set_current(&cli, address, &id, "old");
                c.start_workflow(
                    CrashWorkflow::run,
                    mode.to_string(),
                    WorkflowStartOptions::new(&id, &id).build(),
                )
                .await
                .unwrap();
                let handle = c.get_workflow_handle::<CrashWorkflow>(&id);
                let timer_started = async || {
                    let bytes = handle
                        .fetch_history(WorkflowFetchHistoryOptions::default())
                        .to_json()
                        .await
                        .unwrap();
                    let history = String::from_utf8(bytes).unwrap();
                    assert!(!history.contains("TIMER_FIRED") && !history.contains("TimerFired"));
                    history.contains("TIMER_STARTED") || history.contains("TimerStarted")
                };
                tokio::time::timeout(Duration::from_secs(30), async {
                    loop {
                        if mode == "attempt" && ready.exists() {
                            break;
                        }
                        if mode == "timer" && timer_started().await {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                })
                .await
                .expect("worker did not reach crash point");
                old.0.kill().unwrap();
                assert_eq!(old.0.wait().unwrap().signal(), Some(libc::SIGKILL));
                if mode == "timer" {
                    assert!(timer_started().await);
                }
                let _new = worker(&addr, &id, "new", None);
                set_current(&cli, address, &id, "new");
                // A replacement old-build poller is required: the new current
                // build must not steal an already pinned execution.
                let _replacement = worker(&addr, &id, "old", None);
                if mode == "timer" {
                    let release = handle.signal(CrashWorkflow::release, (), Default::default());
                    release.await.unwrap();
                }
                let result = tokio::time::timeout(
                    Duration::from_secs(120),
                    handle.get_result(WorkflowGetResultOptions::default()),
                )
                .await
                .unwrap()
                .unwrap();
                assert_eq!(result, "old");
            }
            for mode in ["rollover-events", "rollover-waits", "retry-cap"] {
                let id = name(mode);
                let _limits = worker(&addr, &id, "new", None);
                set_current(&cli, address, &id, "new");
                c.start_workflow(
                    CrashWorkflow::run,
                    mode.to_string(),
                    WorkflowStartOptions::new(&id, &id).build(),
                )
                .await
                .unwrap();
                let handle = c.get_workflow_handle::<CrashWorkflow>(&id);
                let result = tokio::time::timeout(
                    Duration::from_secs(90),
                    handle.get_result(WorkflowGetResultOptions::default()),
                )
                .await
                .unwrap()
                .unwrap();
                assert_eq!(
                    result,
                    if mode == "retry-cap" {
                        "capped"
                    } else {
                        "rolled"
                    }
                );
            }
        });
}

fn set_current(cli: &str, address: &str, deployment: &str, build: &str) {
    for _ in 0..100 {
        let output = Command::new(cli)
            .args([
                "worker",
                "deployment",
                "set-current-version",
                "--address",
                address,
                "--deployment-name",
                deployment,
                "--build-id",
                build,
                "--yes",
            ])
            .output()
            .unwrap();
        if output.status.success() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    panic!("deployment {deployment}/{build} did not become current");
}
