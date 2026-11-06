//! Unversioned synthetic process-death regression; NOT pinned-build acceptance.
//! Both children use the same executable. CRASH_BUILD changes activity behavior,
//! not the compiled build identity or Temporal deployment routing. See the open
//! V1/V2 acceptance gaps in docs/plan/rt-p1-wf-versioning-audit.md.
#![cfg(unix)]
use std::{
    path::Path,
    process::{Child, Command, Stdio},
    sync::Arc,
    time::Duration,
};
use temporalio_client::{
    Client, ClientOptions, Connection, ConnectionOptions, Url, WorkflowFetchHistoryOptions,
    WorkflowGetResultOptions, WorkflowStartOptions,
};
use temporalio_macros::{activities, workflow, workflow_methods};
use temporalio_sdk::{
    ActivityOptions, Runtime, Worker, WorkerOptions, WorkflowContext, WorkflowResult,
    activities::{ActivityContext, ActivityError},
};

#[workflow]
#[derive(Default)]
struct CrashWorkflow;
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
            let _ = ctx.timer(Duration::from_secs(3)).await;
        }
        let opts = ActivityOptions::with_start_to_close_timeout(Duration::from_secs(2)).build();
        Ok(ctx.execute_activity(Probe::attempt, (), opts).await?)
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
        if std::env::var("CRASH_BUILD").as_deref() == Ok("old") {
            std::fs::write(std::env::var("CRASH_READY").unwrap(), b"attempt").unwrap();
            tokio::time::sleep(Duration::from_secs(60)).await;
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
fn worker(addr: &str, queue: &str, build: &str, ready: &Path) -> Process {
    Process(
        Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "child_worker", "--nocapture"])
            .env("CRASH_ADDR", addr)
            .env("CRASH_QUEUE", queue)
            .env("CRASH_BUILD", build)
            .env("CRASH_READY", ready)
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    )
}

#[test]
fn sigkill_mid_attempt_and_mid_timer_unversioned_same_binary_recovers() {
    use std::os::unix::process::ExitStatusExt;
    let cli = std::env::var("PG_TEMPORAL_CLI")
        .or_else(|_| std::env::var("TEMPORAL_CLI"))
        .expect(
            "SIGKILL acceptance requires PG_TEMPORAL_CLI or TEMPORAL_CLI; absence is not evidence",
        );
    let temp = tempfile::tempdir().unwrap();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let log = std::fs::File::create(temp.path().join("server.log")).unwrap();
    let _server = Process(
        Command::new(cli)
            .args([
                "server",
                "start-dev",
                "--ip",
                "127.0.0.1",
                "--port",
                &port.to_string(),
                "--headless",
            ])
            .stdout(log.try_clone().unwrap())
            .stderr(log)
            .spawn()
            .unwrap(),
    );
    let addr = format!("http://127.0.0.1:{port}");
    for n in 0..200 {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            break;
        }
        assert!(
            n < 199,
            "server failed: {}",
            std::fs::read_to_string(temp.path().join("server.log")).unwrap()
        );
        std::thread::sleep(Duration::from_millis(100));
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let c = client(&addr).await;
            for mode in ["attempt", "timer"] {
                let ready = temp.path().join(mode);
                let mut old = worker(&addr, mode, "old", &ready);
                c.start_workflow(
                    CrashWorkflow::run,
                    mode.to_string(),
                    WorkflowStartOptions::new(mode, mode).build(),
                )
                .await
                .unwrap();
                let handle = c.get_workflow_handle::<CrashWorkflow>(mode);
                tokio::time::timeout(Duration::from_secs(20), async {
                    loop {
                        if mode == "attempt" && ready.exists() {
                            break;
                        }
                        if mode == "timer" {
                            let bytes = handle
                                .fetch_history(WorkflowFetchHistoryOptions::default())
                                .to_json()
                                .await
                                .unwrap();
                            let history = String::from_utf8(bytes).unwrap();
                            if history.contains("TIMER_STARTED") || history.contains("TimerStarted")
                            {
                                break;
                            }
                        }
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                })
                .await
                .expect("worker did not reach crash point");
                old.0.kill().unwrap();
                assert_eq!(old.0.wait().unwrap().signal(), Some(9));
                let _new = worker(&addr, mode, "new", &ready);
                let result = tokio::time::timeout(
                    Duration::from_secs(30),
                    handle.get_result(WorkflowGetResultOptions::default()),
                )
                .await
                .unwrap()
                .unwrap();
                assert_eq!(result, "new");
            }
            let ready = temp.path().join("limits");
            let _limits = worker(&addr, "limits", "new", &ready);
            for mode in ["rollover-events", "rollover-waits", "retry-cap"] {
                c.start_workflow(
                    CrashWorkflow::run,
                    mode.to_string(),
                    WorkflowStartOptions::new("limits", mode).build(),
                )
                .await
                .unwrap();
                let handle = c.get_workflow_handle::<CrashWorkflow>(mode);
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
