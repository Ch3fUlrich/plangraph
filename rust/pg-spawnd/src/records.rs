//! Read shared records without rewriting them. Python's watchdog/bad-records.json
//! diagnostic output is intentionally not ported; malformed rows are skipped.
use crate::errors::ToolError;
use pg_worker_core::run::record::{run_dir, valid_run_id};
use serde_json::Value;
use std::path::Path;

pub(crate) fn read_record(state: &Path, run_id: &Value) -> Result<Value, ToolError> {
    let id = run_id
        .as_str()
        .filter(|id| valid_run_id(id))
        .ok_or_else(|| ToolError::Value("unknown run id".into()))?;
    let path = run_dir(state, id).map_err(|_| ToolError::Value("unknown run id".into()))?;
    let bytes = std::fs::read(path.join("record.json")).map_err(|e| {
        ToolError::Value(
            if e.kind() == std::io::ErrorKind::NotFound {
                "unknown run id"
            } else {
                "invalid run record"
            }
            .into(),
        )
    })?;
    serde_json::from_slice(&bytes).map_err(|_| ToolError::Value("invalid run record".into()))
}

pub(crate) fn list_runs(state: &Path) -> Vec<Value> {
    let mut paths: Vec<_> = std::fs::read_dir(state.join("runs"))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|e| e.file_name().to_string_lossy().starts_with("run-"))
        .map(|e| e.path().join("record.json"))
        .collect();
    paths.sort();
    let mut runs = Vec::new();
    for path in paths {
        let Some(mut record) = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        else {
            continue;
        };
        if !record.is_object() || !record["run_id"].is_string() {
            continue;
        }
        for (source, dest) in [("parent_session", "parent_session"), ("item", "item_key")] {
            if record.get(dest).is_none() {
                if let Some(value) = record["completion"].get(source).cloned() {
                    record[dest] = value;
                }
            }
        }
        if path.parent().unwrap().join("parent.delivered").exists() {
            record["_completion_acknowledged"] = true.into();
        }
        runs.push(record);
    }
    runs.sort_by(|a, b| {
        b["started"]
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&a["started"].as_f64().unwrap_or(0.0))
    });
    runs
}
