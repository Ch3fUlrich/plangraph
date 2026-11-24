//! What ci-watch did for each red run: one row per run attempt in `red-runs.jsonl`, and the coverage report
//! (`plangraph ci-watch --report`): red runs by class, fix rounds per head by class, red runs without a class
//! and red runs that neither made a fix task nor were rerun as a known flake.
use super::Result;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RunRow {
    pub run_id: u64,
    pub attempt: u64,
    pub head: String,
    pub run_class: String,
    /// Failed jobs: name, job id, class.
    pub jobs: Vec<(String, u64, String)>,
    pub tasks: usize,
    /// `fix-task` or `flake-rerun`.
    pub action: String,
}

pub fn append(dir: &Path, row: &RunRow) -> Result<()> {
    let path = dir.join("red-runs.jsonl");
    let mut bytes = std::fs::read(&path).unwrap_or_default();
    bytes.extend(serde_json::to_vec(row).map_err(|e| e.to_string())?);
    bytes.push(b'\n');
    crate::atomic::write(&path, &bytes).map_err(|e| e.to_string())
}

pub fn load(dir: &Path) -> Result<Vec<RunRow>> {
    match std::fs::read_to_string(dir.join("red-runs.jsonl")) {
        Ok(text) => text
            .lines()
            .map(|l| serde_json::from_str(l).map_err(|_| "invalid red-runs row".into()))
            .collect(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(_) => Err("cannot read red-runs.jsonl".into()),
    }
}

/// The coverage report over the recorded red runs and the automatic-fix ledger.
pub fn report(dir: &Path) -> Result<Value> {
    let rows = load(dir)?;
    let mut by_class: BTreeMap<&str, u64> = BTreeMap::new();
    let mut heads: BTreeMap<&str, BTreeMap<&str, u64>> = BTreeMap::new();
    let (mut unclassified, mut untasked) = (0, 0);
    for row in &rows {
        *by_class.entry(&row.run_class).or_default() += 1;
        *heads
            .entry(&row.head)
            .or_default()
            .entry(&row.run_class)
            .or_default() += 1;
        if row.run_class.is_empty() || row.jobs.iter().any(|(_, _, class)| class.is_empty()) {
            unclassified += 1;
        }
        if row.tasks == 0 && row.action != "flake-rerun" {
            untasked += 1;
        }
    }
    let autofix: super::autofix::Ledger = super::read(&dir.join("autofix.json"))?;
    let pushed = autofix
        .done
        .values()
        .filter(|v| v.contains(": pushed "))
        .count();
    Ok(json!({
        "red_runs": rows.len(),
        "red_runs_by_class": by_class,
        "fix_rounds_per_head_by_class": heads,
        "red_runs_without_class": unclassified,
        "red_runs_without_task": untasked,
        "automatic_fixes_pushed": pushed,
    }))
}
