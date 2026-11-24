//! What a worker's run records say about each run, with its session checked.
use pg_worker_core::run::record::{valid_run_id, Status};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

#[derive(Default)]
pub(crate) struct Inventory {
    /// Running runs whose recorded process still exists, and queued runs the host still holds.
    pub live: BTreeSet<String>,
    /// Runs recorded as running whose process (pid and start time) is gone.
    pub dead: BTreeSet<String>,
    /// Persisted terminal records: status, end time and exit code.
    pub terminal: BTreeMap<String, Value>,
}

/// The recorded process of the run's last attempt, or of the run itself.
fn identity(row: &Value) -> Option<(u32, u64)> {
    let attempt = row["attempts"].as_array().and_then(|a| a.last());
    let proc = attempt
        .map(|attempt| &attempt["proc"])
        .filter(|proc| proc.is_object())
        .unwrap_or(&row["proc"]);
    Some((
        u32::try_from(proc["pid"].as_u64()?).ok()?,
        proc["start_time"].as_u64()?,
    ))
}

#[cfg(target_os = "linux")]
fn alive(pid: u32, start_time: u64) -> bool {
    use pg_worker_core::slots::{GroupLiveness, LinuxLiveness};
    LinuxLiveness::host().process_alive(pid, start_time)
}

/// Without a process probe a recorded run cannot be called dead.
#[cfg(not(target_os = "linux"))]
fn alive(_: u32, _: u64) -> bool {
    true
}

pub(crate) fn scan(
    state: &Path,
    belongs: impl Fn(&Value) -> bool,
    alive: impl Fn(u32, u64) -> bool,
) -> Inventory {
    let mut found = Inventory::default();
    for row in crate::records::list_runs(state) {
        let Some(id) = row["run_id"].as_str().filter(|id| valid_run_id(id)) else {
            continue;
        };
        if !belongs(&row) {
            continue;
        }
        let id = id.to_owned();
        match row["status"].as_str() {
            Some("queued") => {
                found.live.insert(id);
            }
            Some("running" | "unknown") => {
                if identity(&row).is_some_and(|(pid, start)| alive(pid, start)) {
                    found.live.insert(id);
                } else {
                    found.dead.insert(id);
                }
            }
            Some(status) if Status::parse(status).is_some_and(Status::is_terminal) => {
                found.terminal.insert(
                    id,
                    json!({"status": status, "ended": row["ended"], "exit_code": row["exit_code"]}),
                );
            }
            _ => {}
        }
    }
    found
}

pub(crate) fn scan_host(state: &Path, belongs: impl Fn(&Value) -> bool) -> Inventory {
    scan(state, belongs, alive)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn write(state: &Path, id: &str, body: Value) {
        let dir = state.join("runs").join(id);
        fs::create_dir_all(&dir).unwrap();
        let mut row = json!({"run_id": id, "host": "worker"});
        row.as_object_mut()
            .unwrap()
            .extend(body.as_object().unwrap().clone());
        fs::write(dir.join("record.json"), row.to_string()).unwrap();
    }

    #[test]
    fn a_running_record_counts_live_only_while_its_process_exists() {
        let state = tempfile::tempdir().unwrap();
        let state = state.path();
        write(
            state,
            "run-alive",
            json!({"status": "running", "proc": {"pid": 7, "start_time": 70}}),
        );
        write(
            state,
            "run-gone",
            json!({"status": "running", "attempts": [{"proc": {"pid": 8, "start_time": 80}}]}),
        );
        write(state, "run-unproven", json!({"status": "running"}));
        write(state, "run-queued", json!({"status": "queued"}));
        write(
            state,
            "run-done",
            json!({"status": "completed", "ended": 5.0, "exit_code": 0}),
        );
        let found = scan(state, |_| true, |pid, start| (pid, start) == (7, 70));
        assert_eq!(
            found.live,
            ["run-alive", "run-queued"].map(String::from).into()
        );
        assert_eq!(
            found.dead,
            ["run-gone", "run-unproven"].map(String::from).into()
        );
        assert_eq!(found.terminal["run-done"]["exit_code"], 0);
    }
}
