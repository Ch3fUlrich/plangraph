//! Demo driver. `driver all [fixture_dir]` runs every scenario and prints
//! PASS/FAIL. `driver start <mode> <deadline_ms> <id>` / `driver result <id>`
//! are used for the worker-versioning demo.

use std::str::FromStr;
use std::time::{Duration, Instant};
use temporal_spike::{CardInput, CardOutput, TASK_QUEUE, card::Card};
use temporalio_client::{
    Client, ClientOptions, Connection, ConnectionOptions, Url, WorkflowCancelOptions,
    WorkflowFetchHistoryOptions, WorkflowGetResultOptions, WorkflowSignalOptions,
    WorkflowStartOptions,
};

type Handle = temporalio_client::WorkflowHandle<Client, Card>;

fn uid(tag: &str) -> String {
    let n = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("spike-{tag}-{n}")
}

async fn start(
    c: &Client,
    id: &str,
    mode: &str,
    max_attempts: u32,
    deadline_ms: u64,
) -> anyhow::Result<Handle> {
    let input = CardInput {
        mode: mode.into(),
        max_attempts,
        deadline_ms,
    };
    c.start_workflow(
        Card::run,
        input,
        WorkflowStartOptions::new(TASK_QUEUE, id).build(),
    )
    .await?;
    Ok(c.get_workflow_handle::<Card>(id))
}

async fn result(h: &Handle) -> anyhow::Result<CardOutput> {
    Ok(h.get_result(WorkflowGetResultOptions::default()).await?)
}

async fn history_json(h: &Handle) -> anyhow::Result<String> {
    let bytes = h
        .fetch_history(WorkflowFetchHistoryOptions::default())
        .to_json()
        .await?;
    // Strip host-specific identity strings (pid@hostname) before the history is kept.
    let mut v: serde_json::Value = serde_json::from_slice(&bytes)?;
    scrub(&mut v);
    Ok(serde_json::to_string(&v)?)
}

fn scrub(v: &mut serde_json::Value) {
    match v {
        serde_json::Value::Object(m) => {
            for (k, x) in m.iter_mut() {
                if k == "identity" && x.as_str().is_some_and(|s| !s.is_empty()) {
                    *x = "spike-worker".into();
                } else {
                    scrub(x);
                }
            }
        }
        serde_json::Value::Array(a) => a.iter_mut().for_each(scrub),
        serde_json::Value::String(s) => {
            // sticky queue names look like "<pid>@<hostname>-<uuid>"
            if let Some((pid, rest)) = s.split_once('@') {
                if !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()) {
                    let uuid = rest.rsplit('-').next().unwrap_or("");
                    *s = format!("1@spike-{uuid}");
                }
            }
        }
        _ => {}
    }
}

fn report(name: &str, ok: bool, detail: &str) -> bool {
    println!("{} {name}: {detail}", if ok { "PASS" } else { "FAIL" });
    ok
}

async fn all(c: &Client, fixtures: Option<&str>) -> anyhow::Result<()> {
    let mut ok = true;
    let save = |name: &str, json: &str| {
        if let Some(dir) = fixtures {
            std::fs::write(format!("{dir}/{name}.json"), json).expect("write fixture");
        }
    };

    // 1. failing first attempt -> retry; late `answer` signal resumes the wait.
    let h = start(c, &uid("retry"), "fail_first", 3, 20_000).await?;
    tokio::time::sleep(Duration::from_secs(3)).await;
    h.signal(
        Card::answer,
        "yes".to_string(),
        WorkflowSignalOptions::default(),
    )
    .await?;
    let r = result(&h).await?;
    ok &= report(
        "retry+signal",
        r.attempt == "done on attempt 2" && r.resolution == "answer:yes",
        &format!("{r:?}"),
    );
    save("answer", &history_json(&h).await?);

    // 2. heartbeat timeout detected (attempt 1 goes silent), retry succeeds.
    let h = start(c, &uid("stall"), "stall_first", 3, 500).await?;
    let t = Instant::now();
    let r = result(&h).await?;
    let hist = history_json(&h).await?;
    let hb = hist.contains("TIMEOUT_TYPE_HEARTBEAT");
    ok &= report(
        "heartbeat-timeout+retry",
        hb && r.attempt == "done on attempt 2",
        &format!("{r:?} hb_timeout_in_history={hb} took={:?}", t.elapsed()),
    );

    // 3. heartbeat timeout with no retries -> activity failure surfaced to the workflow.
    let h = start(c, &uid("stall1"), "stall_always", 1, 500).await?;
    let r = result(&h).await?;
    ok &= report(
        "heartbeat-timeout-no-retry",
        r.attempt.starts_with("failed:") && r.attempt.contains("imeout"),
        &r.attempt.chars().take(120).collect::<String>(),
    );

    // 4. durable timer wins when nobody answers (5 s) -> default.
    let h = start(c, &uid("deadline"), "ok", 3, 5_000).await?;
    let t = Instant::now();
    let r = result(&h).await?;
    let el = t.elapsed();
    ok &= report(
        "deadline-default",
        r.resolution == "default" && el >= Duration::from_secs(5),
        &format!("{r:?} took={el:?}"),
    );
    save("default", &history_json(&h).await?);

    // 5a. cancel signal during the question wait.
    let h = start(c, &uid("sigcancel"), "ok", 3, 60_000).await?;
    tokio::time::sleep(Duration::from_secs(3)).await;
    h.signal(
        Card::cancel,
        "operator".to_string(),
        WorkflowSignalOptions::default(),
    )
    .await?;
    let r = result(&h).await?;
    ok &= report(
        "cancel-signal-wait",
        r.resolution == "cancelled",
        &format!("{r:?}"),
    );

    // 5b. cancel signal while the activity is running (reaches the activity via heartbeat).
    let h = start(c, &uid("sigcancel2"), "long", 3, 60_000).await?;
    tokio::time::sleep(Duration::from_secs(2)).await;
    h.signal(
        Card::cancel,
        "operator".to_string(),
        WorkflowSignalOptions::default(),
    )
    .await?;
    let r = result(&h).await?;
    ok &= report(
        "cancel-signal-activity",
        r.attempt == "cancelled",
        &format!("{r:?}"),
    );

    // 5c. native workflow cancellation.
    let h = start(c, &uid("wfcancel"), "long", 3, 60_000).await?;
    tokio::time::sleep(Duration::from_secs(2)).await;
    h.cancel(WorkflowCancelOptions::default()).await?;
    let e = result(&h)
        .await
        .err()
        .map(|e| e.to_string())
        .unwrap_or_default();
    ok &= report("workflow-cancel", e.to_lowercase().contains("cancel"), &e);

    println!("{}", if ok { "ALL PASS" } else { "SOME FAILED" });
    if !ok {
        std::process::exit(1);
    }
    Ok(())
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let addr = std::env::var("SPIKE_ADDR").unwrap_or_else(|_| "http://127.0.0.1:7233".into());
    let conn = Connection::connect(ConnectionOptions::new(Url::from_str(&addr)?).build()).await?;
    let c = Client::new(conn, ClientOptions::new("default").build())?;
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["all"] => all(&c, None).await,
        ["all", dir] => all(&c, Some(dir)).await,
        ["start", mode, deadline, id] => {
            start(&c, id, mode, 3, deadline.parse()?).await?;
            println!("started {id}");
            Ok(())
        }
        ["result", id] => {
            let h = c.get_workflow_handle::<Card>(*id);
            println!("{:?}", result(&h).await?);
            Ok(())
        }
        _ => anyhow::bail!(
            "usage: driver all [fixture_dir] | start <mode> <deadline_ms> <id> | result <id>"
        ),
    }
}
