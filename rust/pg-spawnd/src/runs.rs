//! Local runs only. Not ported: served-model witnesses, harness-limit leg booking,
//! writer no-change checks, git config guards, second-leg fall-through, writer
//! continuation, completion events/delivery markers, dispatch-refusal gate, or
//! SPAWNER-LIVENESS-D3622 watchdog. Context packs, inline diffs and writer env
//! belong to the Python launch adapter.
use crate::{errors::ToolError, placement::Args};
use pg_worker_core::run::{
    record::{new_job_id, new_run_id, RunRecord, Status},
    supervise::RunSpec,
    supervisor::{Supervisor, SupervisorError},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

pub(crate) struct Runs {
    pub supervisor: Arc<Supervisor>,
    pub jobs: Mutex<BTreeMap<String, String>>,
}
impl Runs {
    pub fn new(supervisor: Arc<Supervisor>) -> Self {
        Self {
            supervisor,
            jobs: Mutex::new(BTreeMap::new()),
        }
    }
    pub fn spawn(
        &self,
        state: &Path,
        args: &Args,
        host: &str,
        role: Value,
        plan: Value,
    ) -> Result<Value, ToolError> {
        self.spawn_using(state, args, host, role, plan, |state, request| {
            crate::launch::LaunchAdapter::from_env().request(
                state,
                request,
                Duration::from_secs(30),
            )
        })
    }
    fn spawn_using(
        &self,
        state: &Path,
        args: &Args,
        host: &str,
        role: Value,
        plan: Value,
        launch: impl FnOnce(&Path, &Value) -> Result<crate::launch::LaunchSpec, ToolError>,
    ) -> Result<Value, ToolError> {
        let run_id = new_run_id();
        let job_id = new_job_id();
        let dry = args.get("dry_run") == Some(&json!(true));
        let cwd =
            std::env::current_dir().map_err(|_| internal("could not read current directory"))?;
        let leg = plan["first_leg"].clone();
        let launch = launch(
            state,
            &json!({
                "run_id":run_id,"job_id":job_id,"role":role,"dry_run":dry,"leg":leg,
                "prompt":args["prompt"],"card":args["card"],"cwd":cwd.to_string_lossy(),"state":state.to_string_lossy()
            }),
        )?;

        let mut base = json!({"card_hash":crate::pyjson::card_hash(&args["card"]),"role":role,
            "plan":plan,"chosen_leg":null,"request_side_model":null,"witness_kind":null,
            "writer_family":leg["family"],"cwd":launch.cwd.to_string_lossy(),"job_id":job_id});
        if let Some(warnings) = launch.warnings {
            base["warnings"] = json!(warnings);
        }
        let events: Vec<Value> = launch
            .events
            .unwrap_or_default()
            .into_iter()
            .map(Value::Object)
            .collect();
        if dry {
            let mut tail = format!("[dry-run] Planned command: {}", launch.argv.join(" "));
            if let Some(stdin) = &launch.stdin {
                tail.push_str(&format!(" [stdin: {} bytes]", stdin.len()));
            }
            base["stdout_tail"] = json!(tail);
            base["pid"] = Value::Null;
            base["exit_code"] = json!(0);
            base["events"] = json!(events);
            base["chosen_leg"] = leg.clone();
            base["served_model"] = leg["model"].clone();
            base["served_family"] = leg["family"].clone();
            let mut record = create(state, &run_id, host, base, Status::DryRun)?;
            finish(&mut record, Status::DryRun, &leg);
            record
                .save()
                .map_err(|_| internal("could not save run record"))?;
        } else {
            base.as_object_mut().unwrap().remove("job_id");
            let spec = RunSpec {
                run_id: run_id.clone(),
                job_id: job_id.clone(),
                program: launch.argv[0].clone().into(),
                args: launch.argv[1..].iter().map(Into::into).collect(),
                cwd: launch.cwd,
                env: launch
                    .env
                    .into_iter()
                    .map(|(k, v)| (k.into(), v.into()))
                    .collect(),
                stdin: launch.stdin.map(String::into_bytes),
                base: base.as_object().unwrap().clone(),
                events,
                finish: Some(Box::new(move |record, status| finish(record, status, &leg))),
            };
            match self.supervisor.start(spec) {
                Ok(()) => {}
                Err(SupervisorError::QueueFull) => {
                    base["refusal"] = json!("QueueFull");
                    base["error"] = json!("run queue full");
                    base["exit_code"] = json!(2);
                    return Ok(create(state, &run_id, host, base, Status::Refused)?.to_value());
                }
                Err(SupervisorError::DuplicateRun) => {
                    return Err(internal("run id already started by this supervisor"));
                }
                Err(_) => return Err(internal("could not start run")),
            }

        }
        self.jobs.lock().unwrap().insert(job_id, run_id.clone());
        wait(state, &run_id, wait_duration(args.get("wait_seconds")))
    }
}
fn internal(message: &str) -> ToolError {
    ToolError::Internal(message.into())
}
fn create(
    state: &Path,
    id: &str,
    host: &str,
    base: Value,
    status: Status,
) -> Result<RunRecord, ToolError> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    RunRecord::create(
        state,
        id,
        host,
        base.as_object().unwrap().clone(),
        status,
        now,
    )
    .map_err(|_| internal("could not save run record"))
}
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
fn finish(record: &mut RunRecord, status: Status, leg: &Value) {
    let dry = status == Status::DryRun;
    let unwitnessed = json!("unwitnessed");
    record.set("attempts", json!([{"leg":leg,"status":status.as_str(),
        "served_model":if dry { &leg["model"] } else { &unwitnessed },
        "served_family":if dry { &leg["family"] } else { &unwitnessed },
        "served_witness_reason":"no authenticated harness telemetry; skipped or unobserved leg",
        "request_side_model":null,"witness_kind":null,
        "skip_reason":if status == Status::Completed || dry { None } else { record.get("reason") },
        "provider_response":null,"stdout_tail":record.get("stdout_tail"),"stderr_tail":record.get("stderr_tail")} ]));
    record.set(
        "chosen_leg",
        if status == Status::Completed || dry {
            leg.clone()
        } else {
            Value::Null
        },
    );
    record.set("writer_family", leg["family"].clone());
}

#[cfg(all(test, unix))]
#[path = "runs_tests.rs"]
mod tests;

fn wait_duration(value: Option<&Value>) -> Duration {
    #[allow(clippy::manual_clamp)]
    let seconds = value
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .max(0.0)
        .min(3600.0);
    Duration::from_secs_f64(seconds)
}
