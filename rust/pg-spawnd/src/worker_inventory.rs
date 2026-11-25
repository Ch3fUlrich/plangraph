//! What a worker's run records say about each run, with its session checked.
use pg_worker_core::run::record::{valid_run_id, Status};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

#[derive(Default)]
pub(crate) struct Inventory {
    /// Running runs whose recorded process still exists.
    pub live: BTreeSet<String>,
    /// Queued runs the host still holds; no session exists yet.
    pub queued: BTreeSet<String>,
    /// Running runs whose record carries no process identity: neither live nor dead.
    pub unverified: BTreeSet<String>,
    /// Runs recorded as running whose process (pid and start time) is gone.
    pub dead: BTreeSet<String>,
    /// Persisted terminal records: status, end time and exit code.
    pub terminal: BTreeMap<String, Value>,
}

/// The recorded process of the run, in the form its runner writes: an attempt `proc`
/// or the run's own `proc` (supervised runs), top-level `pid` and `pid_start` (native
/// writers), or `external_child` / `external_owner` with `pid` and `start` (fleet sessions).
fn identity(row: &Value) -> Option<(u32, u64)> {
    let attempt = row["attempts"].as_array().and_then(|a| a.last());
    let object = |value: &Value| Some(value).filter(|v| v.is_object()).cloned();
    let (pid, start) = if let Some(proc) = attempt
        .and_then(|attempt| object(&attempt["proc"]))
        .or_else(|| object(&row["proc"]))
    {
        (proc["pid"].clone(), proc["start_time"].clone())
    } else if let Some(proc) =
        object(&row["external_child"]).or_else(|| object(&row["external_owner"]))
    {
        (proc["pid"].clone(), proc["start"].clone())
    } else {
        (row["pid"].clone(), row["pid_start"].clone())
    };
    Some((u32::try_from(pid.as_u64()?).ok()?, start.as_u64()?))
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

/// A fleet session's launcher may exit while the session runs: the roster decides.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub(crate) enum Fleet {
    Running,
    Ended(Value),
    Unknown,
}

pub(crate) fn scan(
    state: &Path,
    belongs: impl Fn(&Value) -> bool,
    alive: impl Fn(u32, u64) -> bool,
    mut fleet: impl FnMut(&Value) -> Fleet,
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
        if row["run_kind"] == "fleet-session"
            && matches!(row["status"].as_str(), Some("running" | "unknown"))
        {
            match fleet(&row) {
                Fleet::Running => found.live.insert(id),
                Fleet::Unknown => found.unverified.insert(id),
                Fleet::Ended(done) => {
                    found.terminal.insert(
                        id,
                        json!({"status": done["status"], "ended": done["ended"],
                               "exit_code": done["exit_code"]}),
                    );
                    true
                }
            };
            continue;
        }
        match row["status"].as_str() {
            Some("queued") => {
                found.queued.insert(id);
            }
            Some("running" | "unknown") => match identity(&row) {
                Some((pid, start)) if alive(pid, start) => {
                    found.live.insert(id);
                }
                Some(_) => {
                    found.dead.insert(id);
                }
                None => {
                    found.unverified.insert(id);
                }
            },
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

#[cfg(target_os = "linux")]
pub(crate) fn scan_host(state: &Path, belongs: impl Fn(&Value) -> bool) -> Inventory {
    use pg_spawnd::external_run::{fleet_state, roster, FleetState};
    // The roster is read once, and only when a fleet session is recorded.
    let mut agents: Option<Option<Vec<Value>>> = None;
    let fleet = |row: &Value| {
        let Some(agents) = agents.get_or_insert_with(|| roster().ok()) else {
            return Fleet::Unknown;
        };
        match fleet_state(row, agents, pg_worker_core::run::finalize::now()) {
            FleetState::Running => Fleet::Running,
            FleetState::Ended(done) => Fleet::Ended(done),
            FleetState::Unknown => Fleet::Unknown,
        }
    };
    scan(state, belongs, alive, fleet)
}

/// Fleet sessions are registered on Linux hosts only; elsewhere none can be proven.
#[cfg(not(target_os = "linux"))]
pub(crate) fn scan_host(state: &Path, belongs: impl Fn(&Value) -> bool) -> Inventory {
    scan(state, belongs, alive, |_| Fleet::Unknown)
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
        // A fleet session whose launcher process is gone but whose roster entry runs, ended, or is unknown.
        write(
            state,
            "run-fleetgone",
            json!({"status": "running", "run_kind": "fleet-session", "fleet_session_name": "w-2",
                   "external_owner": {"pid": 1, "start": 1}}),
        );
        write(
            state,
            "run-fleetlost",
            json!({"status": "running", "run_kind": "fleet-session", "fleet_session_name": "w-3",
                   "external_owner": {"pid": 1, "start": 1}}),
        );
        write(
            state,
            "run-fleet",
            json!({"status": "running", "run_kind": "fleet-session", "fleet_session_name": "w-1",
                   "external_owner": {"pid": 5, "start": 50}, "external_child": {"pid": 6, "start": 60}}),
        );
        write(
            state,
            "run-native",
            json!({"status": "running", "pid": 9, "pid_start": 90}),
        );
        write(
            state,
            "run-nativegone",
            json!({"status": "running", "pid": 9, "pid_start": 91}),
        );
        write(state, "run-queued", json!({"status": "queued"}));
        write(
            state,
            "run-done",
            json!({"status": "completed", "ended": 5.0, "exit_code": 0}),
        );
        let found = scan(
            state,
            |_| true,
            |pid, start| matches!((pid, start), (7, 70) | (9, 90)),
            |row| match row["fleet_session_name"].as_str() {
                Some("w-1") => Fleet::Running,
                Some("w-2") => {
                    Fleet::Ended(json!({"status": "completed", "exit_code": 0, "ended": 9.0}))
                }
                _ => Fleet::Unknown,
            },
        );
        assert_eq!(
            found.live,
            ["run-alive", "run-fleet", "run-native"]
                .map(String::from)
                .into()
        );
        assert_eq!(found.queued, ["run-queued"].map(String::from).into());
        assert_eq!(
            found.dead,
            ["run-gone", "run-nativegone"].map(String::from).into()
        );
        assert_eq!(
            found.unverified,
            ["run-fleetlost", "run-unproven"].map(String::from).into()
        );
        assert_eq!(found.terminal["run-fleetgone"]["status"], "completed");
        assert_eq!(found.terminal["run-done"]["exit_code"], 0);
    }
}
