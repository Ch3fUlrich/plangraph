//! Register out-of-process harnesses in the host lifecycle journal.
use pg_worker_core::run::{
    finalize::now,
    group_unix::proc_start_time,
    lifecycle,
    record::{new_run_id, run_dir, valid_run_id, Status},
    record_io::atomic_write_private,
};
use serde_json::{json, Value};
use std::{
    fs, io,
    path::{Path, PathBuf},
    process,
};

pub const FLEET_OUTCOMES: &str = "fleet-session-outcomes";

fn process_identity(pid: u32) -> Value {
    json!({"pid":pid,"start":proc_start_time(Path::new("/proc"), pid)})
}

fn gone(identity: &Value) -> bool {
    let Some(pid) = identity["pid"]
        .as_u64()
        .and_then(|pid| u32::try_from(pid).ok())
    else {
        return false;
    };
    let Some(start) = identity["start"].as_u64() else {
        return false;
    };
    match proc_start_time(Path::new("/proc"), pid) {
        Some(current) => start != current,
        None => !Path::new("/proc").join(pid.to_string()).exists(),
    }
}

pub struct Registered {
    state: PathBuf,
    path: PathBuf,
    record: Value,
}

impl Registered {
    pub fn start(state: PathBuf, mut record: Value) -> io::Result<Self> {
        let id = new_run_id();
        let path = run_dir(&state, &id)
            .map_err(io::Error::other)?
            .join("record.json");
        record["run_id"] = json!(id);
        record["status"] = json!("running");
        record["started"] = json!(now());
        record["external_owner"] = process_identity(process::id());
        lifecycle::publish(&state, &path, &record)?;
        Ok(Self {
            state,
            path,
            record,
        })
    }

    pub fn id(&self) -> &str {
        self.record["run_id"].as_str().unwrap()
    }

    pub fn child(&mut self, pid: u32) -> io::Result<()> {
        self.save("external_child", process_identity(pid))
    }

    pub fn save(&mut self, key: &str, value: Value) -> io::Result<()> {
        let before = self.record.clone();
        self.record[key] = value;
        lifecycle::publish_recovered(&self.state, &self.path, &before, &self.record)
    }

    pub fn finish(self, status: &str, code: i32, reason: Option<&str>) -> io::Result<()> {
        let finished = terminal(&self.record, status, code, reason, now());
        lifecycle::publish_recovered(&self.state, &self.path, &self.record, &finished)
    }
}

fn terminal(record: &Value, status: &str, code: i32, reason: Option<&str>, ended: f64) -> Value {
    let mut finished = record.clone();
    finished["status"] = json!(status);
    finished["exit_code"] = json!(code);
    finished["ended"] = json!(ended.max(record["started"].as_f64().unwrap_or_default()));
    if let Some(reason) = reason {
        finished["reason"] = json!(reason);
    }
    finished
}

/// Recover only active registrations. A failed roster probe cannot prove a fleet exit.
pub fn recover(state: &Path) -> io::Result<()> {
    let markers = match fs::read_dir(state.join("run-lifecycle/external")) {
        Ok(markers) => markers,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let Some(_guard) =
        pg_worker_core::lockfile::try_lock(&state.join("run-lifecycle/external.lock"))
            .map_err(io::Error::other)?
    else {
        return Ok(());
    };
    let mut fleet = Vec::new();
    for marker in markers {
        let marker = marker?;
        let name = marker.file_name();
        let Some(id) = name.to_str().filter(|id| valid_run_id(id)) else {
            continue;
        };
        let path = state.join("runs").join(id).join("record.json");
        let record: Value = match fs::read(&path)
            .and_then(|bytes| serde_json::from_slice(&bytes).map_err(io::Error::from))
        {
            Ok(record) => record,
            Err(error) => {
                eprintln!("external recovery: could not read record for {id}: {error}");
                continue;
            }
        };
        if record["run_id"] != id {
            continue;
        }
        match record["status"].as_str().and_then(Status::parse) {
            Some(Status::Running) => {}
            Some(status) if status.is_terminal() => {
                match fs::remove_file(marker.path()) {
                    Err(error) if error.kind() != io::ErrorKind::NotFound => {
                        eprintln!(
                            "external recovery: could not remove terminal marker for {id}: {error}"
                        );
                    }
                    _ => {}
                }
                continue;
            }
            _ => continue,
        }
        if record["run_kind"] == "fleet-session" {
            let witness = state.join(FLEET_OUTCOMES).join(format!("{id}.json"));
            match fs::read(&witness) {
                Ok(bytes) => lifecycle::publish_recovered(
                    state,
                    &path,
                    &record,
                    &serde_json::from_slice(&bytes)?,
                )?,
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    fleet.push((path, record, witness));
                }
                Err(error) => return Err(error),
            }
        } else if gone(&record["external_owner"])
            && (record["external_child"].is_null() || gone(&record["external_child"]))
        {
            let finished = terminal(&record, "failed", 1, Some("orphaned"), now());
            lifecycle::publish_recovered(state, &path, &record, &finished)?;
        }
    }
    if !fleet.is_empty() {
        let agents = crate::session::agent_roster()?;
        for (path, record, witness) in fleet {
            // Keep the first daemon observation, including across a crash
            // before the corresponding lifecycle publication.
            if let Some(finished) = fleet_outcome(&record, &agents, now()) {
                atomic_write_private(&witness, &serde_json::to_vec(&finished)?)?;
                lifecycle::publish_recovered(state, &path, &record, &finished)?;
            }
        }
    }
    Ok(())
}

fn fleet_outcome(record: &Value, agents: &[Value], observed: f64) -> Option<Value> {
    let row = agents.iter().find(|row| {
        row["kind"] == "background"
            && if let Some(sid) = record["session_id"].as_str() {
                row["sessionId"] == sid
            } else {
                row["name"] == record["fleet_session_name"]
                    && row["startedAt"].as_f64().is_some_and(|started| {
                        started / 1000.0 >= record["started"].as_f64().unwrap_or_default() - 1.0
                    })
            }
    });
    let Some(row) = row else {
        return (record["session_id"].is_null()
            && observed - record["started"].as_f64()? >= 60.0
            && gone(&record["external_owner"]))
        .then(|| {
            terminal(
                record,
                "failed",
                1,
                Some("fleet_start_unobserved"),
                observed,
            )
        });
    };
    let (status, fallback) = match row["state"].as_str()? {
        "done" | "finished" => ("completed", 0),
        "stopped" | "cancelled" => ("cancelled", 130),
        "failed" | "crashed" => ("failed", 1),
        _ => return None,
    };
    let code = row["exitCode"]
        .as_i64()
        .and_then(|n| i32::try_from(n).ok())
        .unwrap_or(fallback);
    let ended = row["endedAt"].as_f64().map_or(observed, |ts| ts / 1000.0);
    Some(terminal(record, status, code, None, ended))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reaped_process() -> Value {
        let mut child = process::Command::new("/bin/sleep")
            .arg("60")
            .spawn()
            .unwrap();
        let identity = process_identity(child.id());
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(identity["start"].is_number());
        identity
    }

    #[test]
    fn roster_terminal_states_use_exit_and_timestamp_when_reported() {
        let record = json!({"session_id":"fixture","started":1000,"status":"running"});
        for (state, reported, status, code) in [
            ("done", None, "completed", 0),
            ("finished", None, "completed", 0),
            ("stopped", None, "cancelled", 130),
            ("cancelled", Some(143), "cancelled", 143),
            ("failed", None, "failed", 1),
            ("crashed", Some(17), "failed", 17),
        ] {
            let row = json!({"kind":"background","sessionId":"fixture","state":state,
                             "exitCode":reported,"endedAt":1_045_000});
            let outcome = fleet_outcome(&record, &[row], 1070.0).unwrap();
            assert_eq!(outcome["status"], status, "{state}");
            assert_eq!(outcome["exit_code"], code, "{state}");
            assert_eq!(outcome["ended"], 1045.0, "{state}");
        }
        let live = json!({"kind":"background","sessionId":"fixture","state":"working"});
        assert!(fleet_outcome(&record, &[live], 1070.0).is_none());
    }

    #[test]
    fn unobserved_fleet_launch_requires_dead_owner_and_grace_period() {
        let mut record = json!({"started":1000,"status":"running","fleet_session_name":"writer-0001",
                                "external_owner":reaped_process()});
        assert!(fleet_outcome(&record, &[], 1059.0).is_none());
        let outcome = fleet_outcome(&record, &[], 1060.0).unwrap();
        assert_eq!(outcome["status"], "failed");
        assert_eq!(outcome["exit_code"], 1);
        assert_eq!(outcome["reason"], "fleet_start_unobserved");
        assert_eq!(outcome["ended"], 1060.0);

        record["session_id"] = json!("fixture");
        assert!(fleet_outcome(&record, &[], 1060.0).is_none());
        record["session_id"] = Value::Null;
        record["external_owner"] = process_identity(process::id());
        assert!(fleet_outcome(&record, &[], 1060.0).is_none());
    }

    #[test]
    fn external_recovery_keeps_a_live_child_and_records_a_dead_one_once() {
        let state = tempfile::tempdir().unwrap();
        let mut run = Registered::start(
            state.path().to_owned(),
            json!({
                "run_kind":"external-harness","role":"implement","host":"fixture","harness":"codex",
            }),
        )
        .unwrap();
        let gone = reaped_process();
        run.save("external_owner", gone.clone()).unwrap();
        run.child(process::id()).unwrap();
        recover(state.path()).unwrap();
        let path = run.path.clone();
        let snapshot = || serde_json::from_slice::<Value>(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(snapshot()["status"], "running");

        run.save("external_child", gone).unwrap();
        recover(state.path()).unwrap();
        let finished = snapshot();
        assert_eq!(finished["status"], "failed");
        assert_eq!(finished["reason"], "orphaned");
        assert_eq!(finished["exit_code"], 1);
        assert!(finished["ended"].is_number());
        recover(state.path()).unwrap();
        assert_eq!(snapshot(), finished);
        assert_eq!(lifecycle::page(state.path(), 0).unwrap()["next_cursor"], 2);
    }
}
