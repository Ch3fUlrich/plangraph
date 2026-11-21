use serde_json::{json, Value};
use std::{
    io::Write,
    process::{Command, Stdio},
};

pub(super) fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

pub(super) fn record(record: &mut Value, stage: &str, head: &str, started: i64) {
    let Some(batch) = record["item_key"].as_str() else {
        return;
    };
    let ended = now().max(started);
    let row = json!({
        "action": "stage", "batch_key": batch, "stage": stage, "head": head,
        "attempt": started.max(1), "started_at_ms": started, "ended_at_ms": ended,
    });
    if !record["stage_timings"].is_array() {
        record["stage_timings"] = json!([]);
    }
    record["stage_timings"]
        .as_array_mut()
        .unwrap()
        .push(row.clone());
    let Ok(repo) = std::env::var("PG_LANDING_REPO") else {
        return;
    };
    let mut row = row;
    row["repo"] = json!(repo);
    let result = deliver(&row);
    if result.is_err() {
        eprintln!("landing stage {stage} pending keeper delivery");
    }
}

fn deliver(row: &Value) -> Result<(), ()> {
    let mut child = Command::new("plangraph")
        .args(["landing", "journal"])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .map_err(|_| ())?;
    let written = child
        .stdin
        .take()
        .ok_or(())?
        .write_all(row.to_string().as_bytes());
    let status = child.wait().map_err(|_| ())?;
    if written.is_ok() && status.success() {
        Ok(())
    } else {
        Err(())
    }
}
