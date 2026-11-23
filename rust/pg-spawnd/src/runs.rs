//! Local runs; review quota fallthrough is supported; writer checks/continuation, delivery,
//! dispatch refusal and D3622 liveness remain unported; packs/env use Python.
use crate::{errors::ToolError, placement::Args};
use pg_worker_core::run::{
    record::{new_job_id, new_run_id, RunRecord, Status},
    supervise::RunSpec,
    supervisor::{Supervisor, SupervisorError},
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
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
        mut launch_adapter: impl FnMut(&Path, &Value) -> Result<crate::launch::LaunchSpec, ToolError>,
    ) -> Result<Value, ToolError> {
        let (run_id, job_id) = (new_run_id(), new_job_id());
        let admission = match crate::round_progress::admit(state, args) {
            Ok(admission) => admission,
            Err(error) => return crate::round_progress::record_refusal(state, host, args, error),
        };
        let dry = args.get("dry_run") == Some(&json!(true));
        let cwd =
            std::env::current_dir().map_err(|_| internal("could not read current directory"))?;
        let leg = plan["first_leg"].clone();
        let cwd = if role == "review" && crate::cli_review::eligible(&leg) {
            crate::cli_review::cwd(args, cwd)?
        } else {
            cwd
        };
        let launch_req = json!({
            "run_id":run_id,"job_id":job_id,"role":role,"dry_run":dry,"leg":leg,
            "writer_families":args.get("writer_families").cloned().unwrap_or(json!([])),
            "review_of":args.get("review_of"),
            "prompt":args["prompt"],"card":args["card"],"cwd":cwd.to_string_lossy(),"state":state.to_string_lossy()
        });
        let mut launch = match launch_adapter(state, &launch_req) {
            Ok(spec) => spec,
            Err(ToolError::Refused {
                kind: crate::errors::RefusalKind::Spawn,
                ref message,
                ..
            }) if role == "review"
                && message.contains("prompt_too_large:")
                && args["card"]["diff_ref"].get("repo").is_some()
                && args["card"]["diff_ref"].get("repo_id").is_none()
                && args["card"]["diff_ref"].get("paths").is_none()
                && leg["review_mode"] != "inline_only" =>
            {
                drop(admission);
                return crate::review_split::spawn_adapter_split_groups(
                    self,
                    state,
                    args,
                    host,
                    &role,
                    &plan,
                    &leg,
                    &cwd,
                    dry,
                    &mut launch_adapter,
                );
            }
            Err(err) => return Err(err),
        };
        let review = match crate::prompt::prepare_launch(args, &leg, &mut launch, dry) {
            Ok(dispatch) => dispatch,
            Err(ToolError::Refused {
                kind: crate::errors::RefusalKind::Spawn,
                ref message,
                ..
            }) if role == "review"
                && message.contains("prompt_too_large:")
                && args["card"]["diff_ref"].is_object() =>
            {
                let repos = crate::prompt::repos();
                let mode = crate::prompt::configured_mode("review");
                let review_args = json!(args);
                match crate::review_split::plan_review_groups(
                    &review_args,
                    &leg,
                    &launch,
                    &repos,
                    mode == "on",
                ) {
                    Ok(Some(groups)) => {
                        drop(admission);
                        return crate::review_split::spawn_rust_split_groups(
                            self,
                            state,
                            args,
                            host,
                            &role,
                            &plan,
                            &leg,
                            &cwd,
                            dry,
                            &groups,
                            &mut launch_adapter,
                        );
                    }
                    Ok(None) => {
                        return Err(ToolError::Refused {
                            kind: crate::errors::RefusalKind::Spawn,
                            message: message.clone(),
                            legs: vec![],
                        });
                    }
                    Err(
                        e @ (crate::prompt::InlineDiffError::TooLarge
                        | crate::prompt::InlineDiffError::Oversized(_)),
                    ) => {
                        return Err(ToolError::Refused {
                            kind: crate::errors::RefusalKind::Spawn,
                            message: e.to_string(),
                            legs: vec![],
                        });
                    }
                    Err(e) => return Err(internal(&e.to_string())),
                }
            }
            Err(err) => return Err(err),
        };
        let started = self.start_prepared(
            state,
            args,
            host,
            &role,
            &plan,
            &leg,
            &run_id,
            &job_id,
            launch,
            review.as_ref(),
            &admission.fields,
            dry,
            None,
        )?;
        drop(admission);
        if started["status"] == "refused" {
            return Ok(started);
        }
        wait(state, &run_id, wait_duration(args.get("wait_seconds")))
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn start_prepared(
        &self,
        state: &Path,
        args: &Args,
        host: &str,
        role: &Value,
        plan: &Value,
        leg: &Value,
        run_id: &str,
        job_id: &str,
        mut launch: crate::launch::LaunchSpec,
        review: Option<&crate::cli_review::Dispatch>,
        admission_fields: &Value,
        dry: bool,
        group_meta: Option<(&str, usize, usize, &[String])>,
    ) -> Result<Value, ToolError> {
        let mut base = json!({"card_hash":crate::pyjson::card_hash(&args["card"]),"role":role,
            "plan":plan,"chosen_leg":null,"request_side_model":null,"witness_kind":null,
            "writer_family":leg["family"],"cwd":launch.cwd.to_string_lossy(),"job_id":job_id});
        if let Some((seat_group_id, index, total, paths)) = group_meta {
            base["seat_group"] = json!(seat_group_id);
            base["review_group"] = json!({
                "seat_group": seat_group_id,
                "index": index,
                "total": total,
                "paths": paths,
            });
        }
        // D-6652: a Claude writer routed only while the Codex lead is limited records why.
        if plan["reasons"]
            .as_array()
            .is_some_and(|reasons| reasons.iter().any(|r| r == "codex_limited"))
        {
            base["route_reason"] = json!("codex_limited");
        }
        if role == "review" {
            base["review_base"] = args["card"]["diff_ref"]
                .get("base_sha")
                .or_else(|| args["card"]["diff_ref"].get("base"))
                .cloned()
                .unwrap_or(Value::Null);
        }
        if let Some(review) = review {
            review.apply(&mut base);
        }
        let pre_isolate_cwd = launch.cwd.clone();
        let checkout = crate::cli_review::isolate(&mut launch, leg, base["head_sha"].as_str())?;
        if checkout.is_some() {
            base["review_checkout"] = json!(launch.cwd);
        }
        base.as_object_mut()
            .unwrap()
            .extend(admission_fields.as_object().unwrap().clone());
        if !dry && base["writer_round"].is_u64() {
            base["round_work_before"] = json!(crate::round_progress::work_fingerprint(&launch.cwd));
        }
        let allow_shas = allow_shas(args, &launch, &pre_isolate_cwd, base["head_sha"].as_str());
        store_prompt(state, run_id, &mut launch, &mut base, dry, &allow_shas)?;
        if let Some(warnings) = launch.warnings {
            base["warnings"] = json!(warnings);
        }
        let mut events: Vec<Value> = launch
            .events
            .unwrap_or_default()
            .into_iter()
            .map(Value::Object)
            .collect();
        crate::records::annotate_item(&mut base, &args["card"], &mut events);
        if plan["effort"].is_object() {
            base["attempt_capture"] = json!(1);
            if let Some(assigned) = plan["effort"]["lowered"].as_str() {
                events.push(json!({"event":"effort_level_lowered",
                    "ts":pg_worker_core::run::finalize::now(),
                    "item":base["item_key"].as_str().unwrap_or("adhoc"),
                    "assigned":assigned, "reason":plan["effort"]["reason"]}));
            }
        }
        if !dry && crate::gitconfig_guard::writer_role(role) {
            let dir = crate::gitconfig_guard::run_dir(state, run_id)
                .map_err(|_| internal("invalid run id"))?;
            match crate::gitconfig_guard::capture(&launch.cwd, &dir) {
                Ok(armed) => base["gitconfig_guard"] = json!(armed),
                Err(_) => {
                    base["events"] = json!(events);
                    base["reason"] = json!("gitconfig_capture_refused");
                    return Ok(create(state, run_id, host, base, Status::Refused)?.to_value());
                }
            }
        }
        let attempt_id = book_attempt(&mut base, leg);
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
            let mut record = create(state, run_id, host, base, Status::DryRun)?;
            finish(&mut record, Status::DryRun, leg, attempt_id);
            record
                .save()
                .map_err(|_| internal("could not save run record"))?;
        } else {
            base.as_object_mut().unwrap().remove("job_id");
            let hook_group = group_meta.map(|(_, i, t, p)| (i, t, p.to_vec()));
            let spec = RunSpec {
                run_id: run_id.to_owned(),
                job_id: job_id.to_owned(),
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
                events: events.clone(),
                finish: Some(crate::review_retry::hook_with_group(
                    args.clone(),
                    leg.clone(),
                    attempt_id,
                    checkout,
                    allow_shas.clone(),
                    hook_group,
                )),
                allow_shas,
            };
            match self.supervisor.start(spec) {
                Ok(()) => {}
                Err(SupervisorError::QueueFull) => {
                    base["events"] = json!(events);
                    base["refusal"] = json!("QueueFull");
                    base["error"] = json!("run queue full");
                    base["exit_code"] = json!(2);
                    return Ok(create(state, run_id, host, base, Status::Refused)?.to_value());
                }
                Err(SupervisorError::DuplicateRun) => {
                    return Err(internal("run id already started by this supervisor"));
                }
                Err(_) => return Err(internal("could not start run")),
            }
        }
        self.jobs
            .lock()
            .unwrap()
            .insert(job_id.to_owned(), run_id.to_owned());
        Ok(json!({"run_id": run_id, "status": if dry { "dry_run" } else { "running" }}))
    }
}
pub(crate) fn allow_shas(
    args: &Args,
    launch: &crate::launch::LaunchSpec,
    pre_isolate_cwd: &Path,
    review_head: Option<&str>,
) -> BTreeSet<String> {
    let mut shas = BTreeSet::new();
    let diff_ref = args
        .get("card")
        .and_then(|c| c.get("diff_ref"))
        .unwrap_or(&Value::Null);
    let diff_repo = diff_ref
        .get("repo")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .filter(|p| p.is_dir())
        .or_else(|| {
            diff_ref
                .get("repo_id")
                .and_then(Value::as_str)
                .and_then(|id| {
                    crate::prompt::repos()
                        .get(id)
                        .map(|e| PathBuf::from(&e.root))
                })
                .filter(|p| p.is_dir())
        })
        .unwrap_or_else(|| pre_isolate_cwd.to_path_buf());
    for key in ["head", "head_sha", "base", "base_sha"] {
        if let Some(rev) = diff_ref.get(key).and_then(Value::as_str) {
            if let Some(sha) = pg_worker_core::run::scrub::resolve_commit_sha(&diff_repo, rev) {
                shas.insert(sha);
            }
        }
    }
    if let Some(head) = review_head {
        if head.len() == 40 && head.bytes().all(|b| b.is_ascii_hexdigit()) {
            shas.insert(head.to_ascii_lowercase());
        }
    }
    if let Some(prompt) = launch.prompt.as_deref() {
        shas.extend(pg_worker_core::run::scrub::collect_prompt_commit_shas(
            &launch.cwd,
            prompt,
        ));
    }
    shas
}
pub(crate) fn store_prompt(
    state: &Path,
    id: &str,
    launch: &mut crate::launch::LaunchSpec,
    base: &mut Value,
    dry: bool,
    allow_shas: &BTreeSet<String>,
) -> Result<(), ToolError> {
    use sha2::{Digest, Sha256};
    let missing = || internal("launch prompt missing");
    let raw = launch.prompt.as_ref().ok_or_else(missing)?;
    let argv = &launch.argv;
    // Short substrings of flags/model names are not evidence of a task carrier.
    let mut carriers = argv.iter().chain(launch.env.values());
    let exposed = carriers.any(|s| s == raw || (raw.len() >= 16 && s.contains(raw)));
    if !raw.is_empty() && exposed {
        return Err(internal("launch prompt must not be in argv or env"));
    }
    let prompt = pg_worker_core::redact::redact_with_allowlist(raw, allow_shas);
    match launch.stdin.as_mut() {
        Some(stdin) if stdin == raw => *stdin = prompt.clone(),
        Some(stdin) => {
            let mut event: Value =
                serde_json::from_str(stdin).map_err(|_| internal("invalid prompt stream"))?;
            let stream = argv
                .windows(2)
                .any(|w| w == ["--input-format", "stream-json"]);
            if !stream
                || event["event"] != "user"
                || event["message"]["content"].as_str() != Some(raw)
            {
                return Err(internal("launch prompt carriers differ"));
            }
            event["message"]["content"] = json!(prompt);
            *stdin = event.to_string() + "\n";
        }
        None if !raw.is_empty() => return Err(internal("launch prompt carrier missing")),
        None => {}
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
pub(crate) fn wait_many(
    state: &Path,
    ids: &[String],
    timeout: Duration,
) -> Result<Vec<Value>, ToolError> {
    let began = Instant::now();
    loop {
        let mut records = Vec::with_capacity(ids.len());
        let mut all_done = true;
        for id in ids {
            let record = crate::records::read_record(state, &json!(id))?;
            if !terminal(&record) {
                all_done = false;
            }
            records.push(record);
        }
        if all_done || began.elapsed() >= timeout {
            return Ok(records);
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
pub(crate) fn finish(record: &mut RunRecord, status: Status, leg: &Value, attempt_id: u32) {
    finish_with_allowlist(
        record,
        status,
        leg,
        attempt_id,
        &BTreeSet::new(),
        &Value::Null,
    );
}
pub(crate) fn finish_with_allowlist(
    record: &mut RunRecord,
    status: Status,
    leg: &Value,
    attempt_id: u32,
    allow_shas: &BTreeSet<String>,
    card: &Value,
) {
    if crate::cli_review::binding_completed(record) {
        return;
    }
    let diagnostic = crate::gitconfig_guard::guard_diagnostic(record, status, leg, attempt_id);
    let status = crate::gitconfig_guard::enforce(record, status, diagnostic);
    crate::round_progress::finish_round(record, status);
    let mut attempts = match record.get("attempts") {
        None if attempt_id == 1 => vec![json!({"legacy_normalized":true})],
        None => Vec::new(),
        Some(Value::Array(attempts)) => attempts.clone(),
        Some(_) => {
            record.set("attempts_error", json!("attempts must be an array"));
            return;
        }
    };
    let Some(attempt) = attempt_id
        .checked_sub(1)
        .and_then(|index| attempts.get_mut(index as usize))
        .filter(|entry| entry.is_object())
    else {
        record.set("attempts_error", json!("unknown or invalid attempt id"));
        return;
    };
    crate::cli_review::finish(record, status, leg, card);
    let (dry, unwitnessed) = (status == Status::DryRun, json!("unwitnessed"));
    let completion = json!({"leg":leg,"status":status.as_str(),
        "served_model":if dry { &leg["model"] } else { &unwitnessed },
        "served_family":if dry { &leg["family"] } else { &unwitnessed },
        "served_witness_reason":if leg["model"].as_str().is_some_and(|m| m.split('/').next() == Some("omniroute")) {
            "pg-spawnd gateway call-log witness integration unavailable"
        } else { "no authenticated harness telemetry; skipped or unobserved leg" },
        "request_side_model":null,"witness_kind":null,
        "skip_reason":if status == Status::Completed || dry { None } else { record.get("reason") },
        "provider_response":null,"stdout_tail":record.get("stdout_tail"),"stderr_tail":record.get("stderr_tail")});
    let mut completion = completion;
    if record.get("witness_kind").and_then(Value::as_str) == Some("cli_reported") {
        for key in [
            "served_model",
            "served_family",
            "witness_kind",
            "finish_reason",
            "verdict",
        ] {
            completion[key] = record.get(key).cloned().unwrap_or(Value::Null);
        }
    }
    attempt
        .as_object_mut()
        .unwrap()
        .extend(completion.as_object().unwrap().clone());
    record.set("attempts", json!(attempts));
    if record.get("attempts_error").is_some() {
        record.set("attempts_error", Value::Null);
    }
    let chosen = if status == Status::Completed || dry {
        leg.clone()
    } else {
        Value::Null
    };
    record.set("chosen_leg", chosen);
    record.set("writer_family", leg["family"].clone());
    crate::review_retry::witness(record, leg, attempt_id);
    if let Some(state) = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .map(Path::to_owned)
    {
        let value = record.to_value();
        let (rows, skipped) = crate::evidence_row::adapted(&value);
        if !dry {
            record.set("evidence_unclassified_attempts", json!(skipped));
        }
        if value["role"] == "review" {
            let text = crate::review_artifacts::review_text(&state, &value)
                .unwrap_or_else(|_| "Full rendered review unavailable.\nVERDICT: HALT\n".into());
            if !dry && status != Status::Cancelled {
                let answer = crate::review_health::answer(&state, &value);
                record.set(
                    "review_result",
                    json!(crate::review_health::classify(&value, &answer)),
                );
            }
            let result = crate::review_artifacts::write_review_artifacts_with_allowlist(
                &state, &value, &text, allow_shas,
            );
            if result.is_ok() {
                record.set("seat_dir", json!(record.path().with_file_name("seat")));
            } else {
                record.set(
                    "review_artifact_error",
                    json!("review artifacts unavailable"),
                );
            }
        }
        let role = value["role"].as_str().unwrap_or("");
        if let Ok(store) =
            crate::evidence::EvidenceStore::open(&state, crate::evidence::enabled(&state, role))
        {
            if store.backfill(&crate::records::list_runs(&state)).is_err() {
                record.set("evidence_error", json!("evidence unavailable"));
            }
            for row in rows {
                if store.append(&row).is_err() {
                    record.set("evidence_error", json!("evidence unavailable"));
                }
            }
        }
    }
}
#[cfg(all(test, unix))]
#[path = "runs_progress_tests.rs"]
mod progress_tests;
#[cfg(all(test, unix))]
#[path = "runs_record_tests.rs"]
mod record_tests;
#[cfg(all(test, unix))]
#[path = "runs_tests.rs"]
mod tests;
pub(crate) fn wait_duration(value: Option<&Value>) -> Duration {
    #[allow(clippy::manual_clamp)]
    let seconds = value
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .max(0.0)
        .min(3600.0);
    Duration::from_secs_f64(seconds)
}
