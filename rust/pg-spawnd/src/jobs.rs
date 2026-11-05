//! Process-local job views. Catalog counts/default URL remain placeholders (R11).
//! Unlike Python, cancelling a terminal run never rewrites its record.
use crate::{errors::ToolError, placement::Args, runs::Runs};
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

impl Runs {
    fn job_run(&self, id: &str) -> Result<String, ToolError> {
        self.jobs
            .lock()
            .unwrap()
            .get(id)
            .cloned()
            .ok_or_else(|| ToolError::Key(format!("Job {id} not found")))
    }
    fn cancel_run(&self, state: &Path, id: &Value) -> Result<Value, ToolError> {
        let record = crate::records::read_record(state, id)?;
        if crate::runs::terminal(&record) {
            return Ok(record);
        }
        let id = id.as_str().ok_or_else(|| ToolError::Key("run_id".into()))?;
        if !self.supervisor.is_owned(id) {
            return Err(ToolError::Refused {
                kind: crate::errors::RefusalKind::Spawn,
                message: "run is not owned by this daemon; cancel it with the Python spawner"
                    .into(),
                legs: vec![],
            });
        }
        self.supervisor.cancel(id);
        let durations = pg_worker_core::run::supervise::Config::default();
        crate::runs::wait(
            state,
            id,
            durations.grace + durations.kill_timeout + Duration::from_secs(2),
        )
    }
    pub fn job_tool(&self, state: &Path, name: &str, args: &Args) -> Result<Value, ToolError> {
        let required = |key: &str| args.get(key).ok_or_else(|| ToolError::Key(key.into()));
        match name {
            "plangraph_agent_cancel" => {
                if let Some(id) = args.get("run_id") {
                    return self.cancel_run(state, id);
                }
                let id = crate::tools::python_str(required("job_id")?);
                let record = self.cancel_run(state, &json!(self.job_run(&id)?))?;
                Ok(json!({"job_id":id,"status":record["status"]}))
            }
            "plangraph_agent_steer" => {
                let id = crate::tools::python_str(required("job_id")?);
                required("message")?;
                let error = match self.job_run(&id) {
                    Err(_) => "unknown job",
                    Ok(run) => {
                        let record = crate::records::read_record(state, &json!(run))?;
                        if record["status"] == "running" {
                            "stdin closing"
                        } else {
                            "no stdin"
                        }
                    }
                };
                Ok(json!({"ok":false,"steered":false,"error":error}))
            }
            _ => {
                if let Some(id) = args.get("job_id") {
                    let id = crate::tools::python_str(id);
                    return job(state, &id, &self.job_run(&id)?);
                }
                let jobs = self.jobs.lock().unwrap().clone();
                let rows: Result<Vec<_>, _> =
                    jobs.iter().map(|(id, run)| job(state, id, run)).collect();
                Ok(json!({"jobs":rows?}))
            }
        }
    }
    pub fn health_counts(&self, state: &Path) -> (u64, usize) {
        let counts = self.supervisor.counts();
        let dry = self
            .jobs
            .lock()
            .unwrap()
            .values()
            .filter(|id| {
                crate::records::read_record(state, &json!(id))
                    .is_ok_and(|r| r["status"] == "dry_run")
            })
            .count();
        (counts.total_started + dry as u64, counts.running)
    }
}
fn job(state: &Path, id: &str, run: &str) -> Result<Value, ToolError> {
    let r = crate::records::read_record(state, &json!(run))?;
    Ok(json!({"job_id":id,"status":r["status"],"task_prompt":"",
        "model_or_combo":r["plan"]["first_leg"]["model"],"role":r["role"],"risk":"normal",
        "command":[],"start_time":r["started"],"end_time":r["ended"],"exit_code":r["exit_code"],
        "stdout":r["stdout_tail"],"stderr":r["stderr_tail"],"served_model":"unwitnessed",
        "served_family":"unwitnessed","served_witness_reason":r["served_witness_reason"],
        "request_side_model":null,"witness_kind":null,"dry_run":r["status"] == "dry_run",
        "events":r["events"],"steer_messages":[],"cwd":r["cwd"],"last_activity_ts":r["last_activity_ts"]}))
}
