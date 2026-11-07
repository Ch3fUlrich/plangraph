use super::record_io;
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

#[derive(Debug, thiserror::Error)]
pub enum RecordError {
    #[error("bad run id")]
    BadRunId,
    #[error("unknown run id")]
    Unknown,
    #[error("invalid run record")]
    Invalid,
    #[error("event must be an object with a string event")]
    BadEvent,
    #[error("terminal status cannot change")]
    Terminal,
    #[error("invalid status transition")]
    BadTransition,
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Queued,
    Running,
    Completed,
    Failed,
    Cancelled,
    Stalled,
    Timeout,
    Refused,
    Limited,
    DryRun,
    NeedsInput,
    Orphaned,
}

impl Status {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Stalled => "stalled",
            Self::Timeout => "timeout",
            Self::Refused => "refused",
            Self::Limited => "limited",
            Self::DryRun => "dry_run",
            Self::NeedsInput => "needs_input",
            Self::Orphaned => "orphaned",
        }
    }
    pub fn parse(value: &str) -> Option<Self> {
        [
            Self::Queued,
            Self::Running,
            Self::Completed,
            Self::Failed,
            Self::Cancelled,
            Self::Stalled,
            Self::Timeout,
            Self::Refused,
            Self::Limited,
            Self::DryRun,
            Self::NeedsInput,
            Self::Orphaned,
        ]
        .into_iter()
        .find(|s| s.as_str() == value)
    }
    pub fn is_terminal(self) -> bool {
        !matches!(self, Self::Queued | Self::Running)
    }
}

pub fn valid_run_id(id: &str) -> bool {
    id.strip_prefix("run-").is_some_and(|s| {
        (1..=64).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric())
    })
}
pub fn run_dir(state: &Path, id: &str) -> Result<PathBuf, RecordError> {
    if !valid_run_id(id) {
        return Err(RecordError::BadRunId);
    }
    Ok(state.join("runs").join(id))
}
pub fn new_run_id() -> String {
    format!("run-{:032x}", rand::random::<u128>())
}
pub fn new_job_id() -> String {
    format!("job-{:08x}", rand::random::<u32>())
}

pub struct RunRecord {
    fields: Map<String, Value>,
    path: PathBuf,
}
impl RunRecord {
    pub fn create(
        state: &Path,
        run_id: &str,
        host: &str,
        base: Map<String, Value>,
        initial: Status,
        now: f64,
    ) -> Result<Self, RecordError> {
        let path = run_dir(state, run_id)?.join("record.json");
        let mut fields = json!({
            "started": now, "ended": null, "exit_code": null, "host": host,
            "wait_cmd": format!("plangraph-spawn wait {run_id}"),
            "attempts": [], "events": [], "stdout_tail": "", "stderr_tail": "",
            "served_model": "unwitnessed", "served_family": "unwitnessed",
            "served_witness_reason": "no authenticated harness telemetry; skipped or unobserved leg"
        })
        .as_object()
        .unwrap()
        .clone();
        fields.extend(base);
        fields.insert("run_id".into(), json!(run_id));
        fields.insert("status".into(), json!(initial.as_str()));
        if initial.is_terminal() {
            fields.insert("ended".into(), json!(now));
        }
        let record = Self { fields, path };
        record.save()?;
        Ok(record)
    }
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.fields.get(key)
    }
    pub fn set(&mut self, key: &str, value: Value) {
        self.fields.insert(key.into(), value);
    }
    pub fn status(&self) -> Option<Status> {
        Status::parse(self.get("status")?.as_str()?)
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn to_value(&self) -> Value {
        Value::Object(self.fields.clone())
    }
    pub fn push_event(&mut self, value: Value) -> Result<(), RecordError> {
        if !value.is_object() || value.get("event").and_then(Value::as_str).is_none() {
            return Err(RecordError::BadEvent);
        }
        self.fields
            .entry("events")
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or(RecordError::BadEvent)?
            .push(value);
        Ok(())
    }
    pub fn transition(&mut self, to: Status, now: f64) -> Result<(), RecordError> {
        use Status::*;
        let from = self.status().ok_or(RecordError::BadTransition)?;
        if from.is_terminal() {
            return Err(RecordError::Terminal);
        }
        if !matches!(
            (from, to),
            (Queued, Running | Cancelled | Failed | Refused | NeedsInput)
                | (
                    Running,
                    Completed | Failed | Cancelled | Stalled | Timeout | NeedsInput | Orphaned
                )
        ) {
            return Err(RecordError::BadTransition);
        }
        self.set("status", json!(to.as_str()));
        if to.is_terminal() {
            self.set("ended", json!(now));
        }
        if to == Orphaned {
            self.set("exit_code", Value::Null);
        }
        if to == Failed
            && self
                .get("exit_code")
                .is_none_or(|v| v.is_null() || v.as_f64() == Some(0.0))
        {
            self.set("exit_code", json!(1));
        }
        Ok(())
    }
    pub fn save(&self) -> Result<(), RecordError> {
        let bytes = serde_json::to_vec_pretty(&self.fields).map_err(|_| RecordError::Invalid)?;
        Ok(record_io::atomic_write_private(&self.path, &bytes)?)
    }
    pub fn read(state: &Path, run_id: &str) -> Result<Self, RecordError> {
        Self::read_path(run_dir(state, run_id)?.join("record.json"))
    }
    pub fn read_path(path: impl AsRef<Path>) -> Result<Self, RecordError> {
        let path = path.as_ref();
        let text = record_io::read_retry(path).map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                RecordError::Unknown
            } else {
                RecordError::Io(e)
            }
        })?;
        let Value::Object(fields) =
            serde_json::from_str(&text).map_err(|_| RecordError::Invalid)?
        else {
            return Err(RecordError::Invalid);
        };
        Ok(Self {
            fields,
            path: path.into(),
        })
    }
}
