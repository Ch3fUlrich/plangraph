//! Waiting for a run (or several) to reach a terminal status, and the wait budget of a request.
use crate::errors::ToolError;
use pg_worker_core::run::record::Status;
use serde_json::{json, Value};
use std::{
    path::Path,
    time::{Duration, Instant},
};

pub(crate) fn terminal(record: &Value) -> bool {
    record["status"]
        .as_str()
        .and_then(Status::parse)
        .is_some_and(Status::is_terminal)
}
pub(crate) fn wait(state: &Path, id: &str, timeout: Duration) -> Result<Value, ToolError> {
    let began = Instant::now();
    loop {
        let record = crate::records::read_record(state, &json!(id))?;
        if terminal(&record) || began.elapsed() >= timeout {
            return Ok(record);
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
pub(crate) fn wait_many(
    state: &Path,
    ids: &[String],
    timeout: Duration,
) -> Result<Vec<Value>, ToolError> {
    let began = Instant::now();
    loop {
        let mut records = Vec::with_capacity(ids.len());
        let mut all_done = true;
        for id in ids {
            let record = crate::records::read_record(state, &json!(id))?;
            if !terminal(&record) {
                all_done = false;
            }
            records.push(record);
        }
        if all_done || began.elapsed() >= timeout {
            return Ok(records);
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
pub(crate) fn wait_duration(value: Option<&Value>) -> Duration {
    #[allow(clippy::manual_clamp)]
    let seconds = value
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .max(0.0)
        .min(3600.0);
    Duration::from_secs_f64(seconds)
}
