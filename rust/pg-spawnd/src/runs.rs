//! Local runs; witnesses, retry/fall-through, writer checks/continuation, delivery,
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
        let mut launch = launch(
            state,
            &json!({
                "run_id":run_id,"job_id":job_id,"role":role,"dry_run":dry,"leg":leg,
                "prompt":args["prompt"],"card":args["card"],"cwd":cwd.to_string_lossy(),"state":state.to_string_lossy()
            }),
        )?;
        let mut review_args = serde_json::to_value(args).map_err(|_| internal("invalid args"))?;
        review_args["role"] = role.clone();
        let mode = crate::prompt::configured_mode(role.as_str().unwrap_or(""));
        let repos = crate::prompt::repos();
        if mode == "shadow" && leg["review_mode"] != "inline_only" {
            let mut shadow: crate::launch::LaunchSpec = serde_json::from_value(json!({
                "argv":launch.argv,"cwd":launch.cwd,"env":launch.env,"stdin":launch.stdin,
                "prompt":launch.prompt,"warnings":null,"events":null
            }))
            .map_err(|_| internal("invalid shadow launch"))?;
            let would_inline =
                crate::prompt::prepare_review(&review_args, &leg, &mut shadow, &repos, true)
                    .is_ok()
                    && shadow.prompt != launch.prompt;
            eprintln!(
                "inline_review_diff would_inline={}",
                usize::from(would_inline)
            );
        }
        crate::prompt::prepare_review(&review_args, &leg, &mut launch, &repos, mode == "on")
            .map_err(|e| internal(&e.to_string()))?;
        let mut base = json!({"card_hash":crate::pyjson::card_hash(&args["card"]),"role":role,
            "plan":plan,"chosen_leg":null,"request_side_model":null,"witness_kind":null,
            "writer_family":leg["family"],"cwd":launch.cwd.to_string_lossy(),"job_id":job_id});
        store_prompt(state, &run_id, &mut launch, &mut base, dry)?;
        if !dry && crate::gitconfig_guard::writer_role(&role) {
            if crate::gitconfig_guard::writer_preflight::refuse_collection(
                &launch.cwd,
                &args["prompt"],
                &mut base,
            ) {
                return Ok(create(state, &run_id, host, base, Status::Refused)?.to_value());
            }
            let dir = crate::gitconfig_guard::run_dir(state, &run_id)
                .map_err(|_| internal("invalid run id"))?;
            match crate::gitconfig_guard::capture(&launch.cwd, &dir) {
                Ok(armed) => base["gitconfig_guard"] = json!(armed),
                Err(_) => {
                    base["reason"] = json!("gitconfig_capture_refused");
                    return Ok(create(state, &run_id, host, base, Status::Refused)?.to_value());
                }
            }
        }
        if let Some(warnings) = launch.warnings {
            base["warnings"] = json!(warnings);
        }
        let item = args["card"]["item"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .or_else(|| {
                std::env::var("PLANGRAPH_SPAWN_ITEM")
                    .ok()
                    .filter(|s| !s.is_empty())
            });
        base["item_key"] = match item {
            Some(s) if crate::records::valid_item(&s) => {
                json!(s)
            }
            Some(_) => {
                if base.get("warnings").is_none() {
                    base["warnings"] = json!([]);
                }
                base["warnings"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!("invalid item_key; stored as null"));
                Value::Null
            }
            None => Value::Null,
        };
        let events: Vec<Value> = launch
            .events
            .unwrap_or_default()
            .into_iter()
            .map(Value::Object)
            .collect();
        let attempt_id = book_attempt(&mut base, &leg);
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
            finish(&mut record, Status::DryRun, &leg, attempt_id);
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
                finish: Some(Box::new(move |record, status| {
                    finish(record, status, &leg, attempt_id)
                })),
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
pub(crate) fn store_prompt(
    state: &Path,
    id: &str,
    launch: &mut crate::launch::LaunchSpec,
    base: &mut Value,
    dry: bool,
) -> Result<(), ToolError> {
    use sha2::{Digest, Sha256};
    let raw = launch
        .prompt
        .as_ref()
        .ok_or_else(|| internal("launch prompt missing"))?;
    let mut carriers = launch
        .stdin
        .iter()
        .chain(launch.argv.iter())
        .chain(launch.env.values())
        .filter(|s| !raw.is_empty() && s.contains(raw));
    let channel = if raw.is_empty() {
        String::new()
    } else {
        carriers
            .next()
            .ok_or_else(|| internal("launch prompt carrier missing"))?
            .clone()
    };
    if carriers.any(|s| s != &channel) {
        return Err(internal("launch prompt carriers differ"));
    }
    let redacted = pg_worker_core::redact::redact(raw);
    for arg in launch.argv.iter_mut().chain(launch.env.values_mut()) {
        *arg = arg.replace(raw, &redacted);
    }
    if let Some(stdin) = &mut launch.stdin {
        *stdin = stdin.replace(raw, &redacted);
    }
    if let Some(warnings) = &mut launch.warnings {
        for warning in warnings {
            *warning = pg_worker_core::redact::redact(warning);
        }
    }
    if let Some(events) = &mut launch.events {
        for event in events {
            let clean = pg_worker_core::redact::redact(&Value::Object(event.clone()).to_string());
            *event = serde_json::from_str(&clean)
                .map_err(|_| internal("could not redact launch event"))?;
        }
    }
    let prompt = channel.replace(raw, &redacted);
    let path = pg_worker_core::run::record::run_dir(state, id)
        .map_err(|_| internal("invalid run id"))?
        .join("prompt.txt");
    if !dry {
        pg_worker_core::run::record_io::atomic_write_private(&path, prompt.as_bytes())
            .map_err(|_| internal("could not save prompt"))?;
    }
    base["prompt_sha256"] = json!(format!("{:x}", Sha256::digest(prompt.as_bytes())));
    base["wake_variant"] = json!("initial");
    launch.prompt = Some(prompt);
    Ok(())
}
fn internal(message: &str) -> ToolError {
    ToolError::Internal(message.into())
}
pub(crate) fn create(
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
fn book_attempt(base: &mut Value, leg: &Value) -> u32 {
    if base.get("attempts").is_none() {
        base["attempts"] = json!([]);
    }
    let attempts = base["attempts"].as_array_mut().unwrap();
    attempts.push(json!({"leg":leg,"status":"running"}));
    u32::try_from(attempts.len()).expect("attempt count fits u32")
}
#[path = "runs_completion.rs"]
mod completion;
pub(crate) use completion::finish;
#[cfg(all(test, unix))]
#[path = "runs_record_tests.rs"]
mod record_tests;
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
