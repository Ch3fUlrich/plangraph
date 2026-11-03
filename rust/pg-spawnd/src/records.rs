//! Read shared records without rewriting them. Python's watchdog/bad-records.json
//! diagnostic output is intentionally not ported; malformed rows are skipped.
use crate::errors::ToolError;
use pg_worker_core::run::record::{run_dir, valid_run_id};
use serde_json::Value;
use std::io::Read;
use std::path::Path;

const MAX_RECORD_BYTES: u64 = 8 * 1024 * 1024;

fn read_record_file(path: &Path) -> Result<Value, ToolError> {
    let invalid = || ToolError::Value("invalid run record".into());
    let read = || -> std::io::Result<Vec<u8>> {
        let metadata = std::fs::symlink_metadata(path)?;
        if !metadata.is_file() || metadata.len() > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("invalid run record"));
        }
        let file = std::fs::File::open(path)?;
        if !file.metadata()?.is_file() {
            return Err(std::io::Error::other("invalid run record"));
        }
        let mut bytes = Vec::new();
        file.take(MAX_RECORD_BYTES + 1).read_to_end(&mut bytes)?;
        Ok(bytes)
    };
    let bytes = read().map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            ToolError::Value("unknown run id".into())
        } else {
            invalid()
        }
    })?;
    if bytes.len() as u64 > MAX_RECORD_BYTES {
        return Err(invalid());
    }
    serde_json::from_slice(&bytes).map_err(|_| invalid())
}

pub(crate) fn read_record(state: &Path, run_id: &Value) -> Result<Value, ToolError> {
    let id = run_id
        .as_str()
        .filter(|id| valid_run_id(id))
        .ok_or_else(|| ToolError::Value("unknown run id".into()))?;
    let path = run_dir(state, id).map_err(|_| ToolError::Value("unknown run id".into()))?;
    read_record_file(&path.join("record.json"))
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
        let Ok(mut record) = read_record_file(&path) else {
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
