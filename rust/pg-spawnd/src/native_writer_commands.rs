use super::{native_writer, writer_effort, Driver, Host, WRITER_EFFORT};
use pg_worker_core::run::record::{run_dir, valid_run_id};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};

fn read_record(path: &Path) -> Result<Value, String> {
    crate::records::read_record_file(path)
        .map_err(|error| error.into_error_data().message.into_owned())
}

fn mark_process(record: &mut Value) {
    record["pid"] = json!(std::process::id());
    record.as_object_mut().unwrap().remove("pid_start");
    #[cfg(target_os = "linux")]
    if let Ok(stat) = fs::read_to_string("/proc/self/stat") {
        if let Some(ticks) = stat
            .rsplit_once(')')
            .and_then(|(_, tail)| tail.split_whitespace().nth(19))
            .and_then(|s| s.parse::<u64>().ok())
        {
            record["pid_start"] = json!(ticks);
        }
    }
}

impl Host {
    fn from_request(request: &Value, record: &Value, record_path: PathBuf) -> Result<Self, String> {
        let state = record_path
            .ancestors()
            .nth(3)
            .ok_or("record_path has no state root")?
            .to_owned();
        let cwd = PathBuf::from(record["cwd"].as_str().ok_or("worktree path missing")?);
        let reviews = record["review_dir"]
            .as_str()
            .or_else(|| request["card"]["review_dir"].as_str())
            .map(PathBuf::from)
            .unwrap_or_else(|| cwd.join("reviews"));
        Ok(Self {
            repo: PathBuf::from(record["repo"].as_str().ok_or("repo missing")?),
            run_id: record["run_id"]
                .as_str()
                .filter(|id| valid_run_id(id))
                .ok_or("invalid run id")?
                .into(),
            cwd,
            reviews,
            record_path,
            base_ref: record["base_ref"].as_str().unwrap_or("").into(),
            tool_path: record["tool_path"]
                .as_str()
                .ok_or("tool PATH missing")?
                .into(),
            effort: WRITER_EFFORT.into(),
            state,
            brief: request["prompt"].as_str().unwrap_or("").into(),
            push_ref: request["card"]["push_ref"].as_str().map(str::to_owned),
            reviews_started: 0,
            review_override: None,
            review_cli: super::review::review_cli()?,
            python_src: None,
        })
    }

    fn configure(&mut self, request: &Value) -> Result<(), String> {
        super::check_push_ref(&request["card"])?;
        self.brief = pg_spawnd::batch_writer_brief::render(
            request["prompt"].as_str().ok_or("prompt missing")?,
        )?
        .into_owned();
        self.effort = writer_effort(&request["card"])?;
        self.review_override = super::review_route::review_override(&request["card"])?;
        Ok(())
    }
}

pub(crate) fn execute(args: Vec<String>) -> Result<(), String> {
    if cfg!(windows) {
        return Err("native writer requires the POSIX overflow transport in slice 1".into());
    }
    let [request_path] = args.as_slice() else {
        return Err("native-writer requires a private request file".into());
    };
    let request = read_record(Path::new(request_path))?;
    let record_path = PathBuf::from(
        request["record_path"]
            .as_str()
            .ok_or("record_path missing")?,
    );
    let _owner = crate::writer_control::supervisor_guard(&record_path)?;
    let mut record = request["record"].clone();
    let directory = record_path.parent().ok_or("record directory missing")?;
    let id = record["run_id"]
        .as_str()
        .filter(|id| valid_run_id(id))
        .ok_or("invalid run id")?;
    let default = directory.join("worktree");
    let cwd = match pg_worker_core::work_disks::worktree_disk() {
        Ok(Some((root, _))) => root.join(id),
        Ok(None) => default,
        Err(error) => {
            record["cwd"] = json!(default);
            record["status"] = json!("refused");
            record["exit_code"] = json!(8);
            record["reason"] = json!(error.to_string());
            return pg_worker_core::run::record_io::atomic_write_private(
                &record_path,
                record.to_string().as_bytes(),
            )
            .map_err(|error| error.to_string());
        }
    };
    record["cwd"] = json!(cwd);
    execute_request(&request, record)
}

pub(crate) fn resume_saved(args: Vec<String>) -> Result<(), String> {
    let [record] = args.as_slice() else {
        return Err("usage: pg-spawnd writer-resume RECORD".into());
    };
    let record = Path::new(record);
    let request_path = record
        .parent()
        .ok_or("writer record directory missing")?
        .join("native-request.json");
    let request: Value = read_record(&request_path)?;
    let requested = request["record_path"]
        .as_str()
        .ok_or("writer request record path missing")?;
    if Path::new(requested)
        .canonicalize()
        .ok()
        .zip(record.canonicalize().ok())
        .is_none_or(|(a, b)| a != b)
    {
        return Err("saved writer request names another record".into());
    }
    let _supervisor = crate::writer_control::supervisor_guard(record)?;
    let mut resumed = crate::writer_control::take_resume(record)?;
    resumed.as_object().ok_or("invalid saved writer record")?;
    native_writer::clear_ended(&mut resumed, native_writer::SAVED_REOPEN_FIELDS);
    resumed["status"] = json!("running");
    execute_request(&request, resumed)
}

fn execute_request(request: &Value, mut record: Value) -> Result<(), String> {
    let record_path = PathBuf::from(
        request["record_path"]
            .as_str()
            .ok_or("record_path missing")?,
    );
    mark_process(&mut record);
    pg_spawnd::lane_guard::publish_marker(request, &mut record);
    let mut host = Host::from_request(request, &record, record_path)?;
    if let Err(reason) = host.configure(request) {
        record["status"] = json!("failed");
        record["exit_code"] = json!(5);
        record["reason"] = json!(reason);
        return host.save(&record);
    }
    if let Some((leg, reason)) = &host.review_override {
        record["requested_review_leg"] = json!(leg);
        record["review_override_reason"] = json!(reason);
    }
    record["effort"] = json!(host.effort);
    record["requested_model"] = json!(super::WRITER_MODEL);
    if record["plan"].is_object() {
        record["plan"]["legs"] = json!([{"id":"native-codex", "model":super::WRITER_MODEL,
                                         "harness":"codex", "effort":host.effort}]);
        record["plan"]["static_v0"] = json!("native-codex");
    }
    record["cwd"] = json!(host.cwd);
    for field in ["writer_status_path", "writer_slices_path"] {
        if let Some(path) = request["card"][field].as_str() {
            record[field] = json!(path);
        }
    }
    host.save(&record)?;
    let brief = host.brief.clone();
    native_writer::run(&mut host, record, &brief);
    Ok(())
}

fn read_prompt(path: &Path) -> Result<String, String> {
    let mut text = String::new();
    crate::records::open_record(path)
        .and_then(|file| file.take(1024 * 1024 + 1).read_to_string(&mut text))
        .map_err(|e| format!("resume prompt unreadable: {e}"))?;
    if text.trim().is_empty() || text.len() > 1024 * 1024 {
        return Err("resume prompt must contain between 1 byte and 1 MiB of text".into());
    }
    Ok(text)
}

fn reopen(state: &Path, id: &str, prompt_path: &Path) -> Result<Value, String> {
    let directory = run_dir(state, id).map_err(|_| "unknown run id")?;
    let path = directory.join("record.json");
    let _owner = crate::writer_control::supervisor_guard(&path)?;
    let mut record = read_record(&path)?;
    if record["run_id"] != id
        || record["run_kind"] != "native-codex"
        || record["overflow_remote"] == true
        || record["origin_receipt"] == true
    {
        return Err("resume must run on the native writer's owning host".into());
    }
    if !matches!(record["status"].as_str(), Some("completed" | "failed"))
        || !record["ended"].is_number()
        || record["survivors"]
            .as_array()
            .is_some_and(|rows| !rows.is_empty())
    {
        return Err("native writer has no safely ended run to resume".into());
    }
    let session = record["session_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("run has no Codex session")?
        .to_owned();
    let request = read_record(&directory.join("native-request.json"))?;
    if request["record"]["run_id"] != id {
        return Err("native request belongs to another run".into());
    }
    let mut host = Host::from_request(&request, &record, path)?;
    host.configure(&request)?;
    let head = host.head()?;
    if record["final_head"] != head {
        return Err("native worktree HEAD differs from the ended run".into());
    }
    if host.disk_gb()? < 5 {
        return Err("disk_low: need 5 GB free to start".into());
    }
    let prompt = read_prompt(prompt_path)?;
    let log_offset = fs::metadata(directory.join("native.log"))
        .map_err(|e| format!("native session log unavailable: {e}"))?
        .len();
    host.reviews_started = record["reviews"].as_array().map_or(0, Vec::len);
    record["resume_from"] = json!({
        "head":head,"status":record["status"],"started":record["started"],
        "ended":record["ended"],"exit_code":record["exit_code"],"log_offset":log_offset
    });
    record["resume_number"] = json!(record["resume_number"]
        .as_u64()
        .unwrap_or(0)
        .checked_add(1)
        .ok_or("resume number overflow")?);
    record["status"] = json!("running");
    record["started"] = json!(chrono::Utc::now().timestamp_millis() as f64 / 1000.0);
    record["review_rounds"] = json!(0);
    native_writer::clear_ended(&mut record, native_writer::RESUME_REOPEN_FIELDS);
    mark_process(&mut record);
    host.save(&record)?;
    Ok(native_writer::run_resume(
        &mut host, record, &session, &prompt,
    ))
}

pub(crate) fn resume(args: Vec<String>, output: &mut dyn Write) -> Result<(), String> {
    let usage = "usage: pg-spawnd resume RUN_ID --prompt-file PATH [--state PATH]";
    let mut args = args.iter().map(String::as_str);
    let id = args.next().ok_or(usage)?;
    if id == "--help" || id == "-h" {
        return writeln!(output, "{usage}").map_err(|e| e.to_string());
    }
    let mut prompt_path = None;
    let mut state = None;
    while let Some(flag) = args.next() {
        match flag {
            "--prompt-file" if prompt_path.is_none() => {
                prompt_path = Some(args.next().ok_or(usage)?)
            }
            "--state" if state.is_none() => state = Some(args.next().ok_or(usage)?),
            _ => return Err(usage.into()),
        }
    }
    let state = crate::state::resolve_state(state, |key| std::env::var_os(key))?;
    let prompt = Path::new(prompt_path.ok_or(usage)?);
    let directory = run_dir(&state, id).map_err(|_| "unknown run id")?;
    if !directory.join("record.json").exists() {
        return Err("unknown run id".into());
    }
    let record = reopen(&state, id, prompt)?;
    writeln!(output, "{record}").map_err(|e| e.to_string())?;
    if record["status"] != "completed" {
        return Err(record["reason"]
            .as_str()
            .or_else(|| record["round_stop"].as_str())
            .unwrap_or("native resume failed")
            .into());
    }
    Ok(())
}
