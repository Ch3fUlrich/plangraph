//! Register out-of-process harnesses in the host lifecycle journal.
use pg_worker_core::run::{
    finalize::now,
    group_unix::proc_start_time,
    lifecycle,
    record::{new_run_id, run_dir, valid_run_id},
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
        let record: Value = serde_json::from_slice(&fs::read(&path)?)?;
        if record["status"] != "running" || record["run_id"] != id {
            continue;
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
