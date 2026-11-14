//! Long-lived package sessions. Keeper claims precede package construction.
use crate::{
    errors::ToolError,
    placement::{parameter, Args},
};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
#[derive(serde::Deserialize, Clone)]
pub(crate) struct Host {
    pub kind: String,
    pub target: String,
    pub repo: String,
    pub root: String,
}
pub(crate) trait Backend {
    fn keeper(&mut self, verb: &str, key: &str) -> Result<Value, ToolError>;
    fn remote(&mut self, host: &Host, script: &str) -> Result<Value, ToolError>;
}
pub(crate) fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn string<'a>(args: &'a Args, name: &str) -> Result<&'a str, ToolError> {
    args.get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| parameter(format!("{name} must be a nonempty string")))
}
fn safe_ref(s: &str) -> bool {
    s.len() <= 180
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"/._-".contains(&c))
        && !s.contains("..")
        && !s.contains("//")
        && !s.ends_with(['/', '.'])
        && !s.contains("/.")
        && !s.ends_with(".lock")
}
pub(crate) fn effort(args: &Args) -> Result<&str, ToolError> {
    let effort = args
        .get("effort")
        .and_then(Value::as_str)
        .unwrap_or("medium");
    if !["low", "medium", "high", "xhigh", "max"].contains(&effort) {
        return Err(parameter("invalid reasoning effort"));
    }
    let card = args.get("card").unwrap_or(&Value::Null);
    if ["high", "xhigh", "max"].contains(&effort)
        && !(card["effort"] == effort
            && card["effort_decision"]
                .as_str()
                .is_some_and(|s| regex::Regex::new(r"^D-[0-9]+$").unwrap().is_match(s)))
    {
        return Err(parameter(
            "higher effort requires explicit card effort and D-id (effort_decision)",
        ));
    }
    Ok(effort)
}
pub(crate) fn admit_leg_effort(args: &Args, leg: &Value) -> Result<(), ToolError> {
    let model = leg["model"].as_str().unwrap_or("");
    let suffix = ["xhigh", "high", "max"]
        .into_iter()
        .find(|e| model.ends_with(&format!("-{e}")));
    for effective in [leg["effort"].as_str(), suffix].into_iter().flatten() {
        let mut args = args.clone();
        args.insert("effort".into(), json!(effective));
        effort(&args)?;
    }
    Ok(())
}
fn path(state: &Path, id: &str) -> Result<PathBuf, ToolError> {
    if !pg_worker_core::run::record::valid_run_id(id) {
        return Err(parameter("invalid package run id"));
    }
    Ok(state.join("package-runs").join(id).join("record.json"))
}
pub(crate) fn read(state: &Path, id: &str) -> Result<Value, ToolError> {
    let bytes = std::fs::read(path(state, id)?).map_err(|_| parameter("unknown package run"))?;
    serde_json::from_slice(&bytes).map_err(|_| ToolError::Internal("invalid package record".into()))
}
pub(crate) fn save(state: &Path, run: &Value) -> Result<(), ToolError> {
    let path = path(
        state,
        run["run_id"]
            .as_str()
            .ok_or_else(|| parameter("missing run id"))?,
    )?;
    let write = || -> std::io::Result<()> {
        std::fs::create_dir_all(path.parent().unwrap())?;
        let temp =
            path.with_extension(format!("{}.tmp", pg_worker_core::run::record::new_run_id()));
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        use std::io::Write;
        let mut file = opts.open(&temp)?;
        file.write_all(serde_json::to_string_pretty(run)?.as_bytes())?;
        file.sync_all()?;
        std::fs::rename(&temp, &path)
    };
    write().map_err(|_| ToolError::Internal("could not save package record".into()))
}
pub(crate) fn view(mut run: Value) -> Value {
    if let Some(object) = run.as_object_mut() {
        object.remove("package_text");
        object.remove("snapshot");
    }
    run
}
pub(crate) fn list(state: &Path) -> Vec<Value> {
    let mut runs: Vec<_> = std::fs::read_dir(state.join("package-runs"))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|e| read(state, &e.file_name().to_string_lossy()).ok())
        .collect();
    runs.sort_by(|a, b| a["run_id"].as_str().cmp(&b["run_id"].as_str()));
    runs
}
pub(crate) fn configured_host(state: &Path, name: &str) -> Result<Host, ToolError> {
    let config = std::env::var_os("PG_PACKAGE_HOSTS")
        .map(PathBuf::from)
        .unwrap_or_else(|| state.join("package-hosts.json"));
    let bytes =
        std::fs::read(config).map_err(|_| parameter("package host configuration unavailable"))?;
    let config: Value = serde_json::from_slice(&bytes)
        .map_err(|_| parameter("invalid package host configuration"))?;
    let host: Host = serde_json::from_value(config["hosts"][name].clone())
        .map_err(|_| parameter("unknown or invalid package host"))?;
    if !["linux", "windows"].contains(&host.kind.as_str())
        || host.root.is_empty()
        || host.repo.is_empty()
        || host.target.is_empty()
        || host.target.starts_with('-')
        || host.target.chars().any(char::is_whitespace)
    {
        return Err(parameter("invalid package host configuration"));
    }
    Ok(host)
}
fn launch(
    state: &Path,
    args: &Args,
    host: &Host,
    backend: &mut impl Backend,
) -> Result<Value, ToolError> {
    let host_name = string(args, "host")?;
    let base = string(args, "base_ref")?;
    let branch = string(args, "branch")?;
    if !safe_ref(base)
        || !(base == "origin/main" || base.starts_with("origin/phase/"))
        || !safe_ref(branch)
        || !branch.starts_with("phase/")
    {
        return Err(parameter("invalid package base_ref or branch"));
    }
    let effort = effort(args)?;
    let model = args
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("gpt-6.1-sol");
    if model.starts_with('-')
        || !model
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
    {
        return Err(parameter("invalid package model"));
    }
    admit_leg_effort(args, &json!({"model":model,"effort":effort}))?;
    let keys = args
        .get("keys")
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty() && a.len() <= 100)
        .ok_or_else(|| parameter("keys must contain 1-100 keeper keys"))?;
    if keys
        .iter()
        .any(|v| !v.as_str().is_some_and(crate::records::valid_item))
    {
        return Err(parameter("invalid keeper key"));
    }
    if args
        .get("extra_text")
        .is_some_and(|s| !s.is_string() || s.as_str().unwrap().len() > 64 * 1024)
    {
        return Err(parameter("invalid extra_text"));
    }
    let mut accepted = Vec::new();
    let mut refused = Vec::new();
    let mut items = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for key in keys.iter().map(|v| v.as_str().unwrap()) {
        if !seen.insert(key) {
            continue;
        }
        let claim = backend.keeper("claim", key)?;
        if claim["status"] == "refused" || claim.get("error").is_some() {
            refused.push(json!({"key":key,"reason":claim.get("reason").or_else(||claim.get("error")).cloned().unwrap_or(json!("keeper refused"))}));
            continue;
        }
        let shown = backend.keeper("show", key)?;
        if shown["status"] == "refused" {
            return Err(parameter("claimed item could not be read"));
        }
        let item = shown.get("item").unwrap_or(&shown);
        items.push(format!(
            "## {key}: {}\n\n{}\n\nDone: {}\n",
            item["title"].as_str().unwrap_or(""),
            item["description"].as_str().unwrap_or(""),
            item["done_criteria"].as_str().unwrap_or("")
        ));
        accepted.push(key.to_owned());
    }
    if accepted.is_empty() {
        return Ok(
            json!({"status":"refused","keys":[],"refused":refused,"reason":"no keeper claims accepted"}),
        );
    }
    let id = pg_worker_core::run::record::new_run_id();
    let sep = if host.kind == "windows" { '\\' } else { '/' };
    let work = format!("{}{sep}{id}", host.root.trim_end_matches(['/', '\\']));
    let package = format!("{work}{sep}PACKAGE-{id}.md");
    let status = format!("{work}{sep}STATUS-package-{id}.txt");
    let text=format!("# Package {id}\n\nExecute tasks in order, effort {effort}. Do not push.\nNew production code in Rust only; existing Python fixes and MCP glue allowed. File cap 800.\nRun affected tests only, in tmp roots with fakes; never SSH or use the real repository Git metadata in tests.\nNo host names in tracked text; use Linux worker host / Windows worker host.\nUse the worker account's configured Git name and noreply email; pass both explicitly with -c user.name and -c user.email when committing.\nEach commit: Plan-Item: <keeper key> and Writer: codex/{model}-{effort} (native CLI, D-5840).\nKeep {status} untracked. Each task: time key DONE|SKIPPED|REFUTED sha +/- tests failing-first choice. Finish with ALL DONE.\nRead STEER before each task.\n\n{}\n{}",items.join("\n"),args.get("extra_text").and_then(Value::as_str).unwrap_or(""));
    let mut run = json!({"run_id":id,"agent_name":format!("pkg-{}", &id[4..28]),"kind":"package","host":host_name,"platform":host.kind,"base_ref":base,"branch":branch,"worktree":work,
        "keys":accepted,"refused":refused,"model":model,"effort":effort,"effort_decision":args.get("card").and_then(|c|c.get("effort_decision")),"package_path":package,"package_text":text,
        "status_path":status,"log_path":format!("{work}{sep}package.log"),"steer_path":format!("{work}{sep}STEER"),
        "pid_path":format!("{work}{sep}package.pid"),"runner_path":format!("{work}{sep}package.ps1"),"status":"launching","started":now()});
    save(state, &run)?;
    let launched = (|| {
        let mut result = backend.remote(host, &crate::package_scripts::launch(host, &run))?;
        if host.kind == "linux" {
            let pane = result
                .pointer("/result/root_pane/pane_id")
                .or_else(|| result.get("pane"))
                .and_then(Value::as_str)
                .ok_or_else(|| parameter("herdr returned no root pane"))?;
            run["pane"] = json!(pane);
            save(state, &run)?;
            result = backend.remote(host, &crate::package_scripts::start_linux(&run))?;
        }
        if result["alive"] != true {
            return Err(parameter("package did not become alive"));
        }
        Ok(result)
    })();
    match launched {
        Ok(result) => {
            run.as_object_mut().unwrap().extend(
                result
                    .as_object()
                    .ok_or_else(|| parameter("invalid launch reply"))?
                    .clone(),
            );
            run["status"] = json!("running");
        }
        Err(error) => {
            run["status"] = json!("failed");
            save(state, &run)?;
            return Err(error);
        }
    }
    save(state, &run)?;
    Ok(run)
}
pub(crate) fn handle(state: &Path, name: &str, args: &Args) -> Result<Value, ToolError> {
    let mut backend = crate::package_transport::Native::default();
    match name {
        "plangraph_package_launch" => {
            let host = configured_host(state, string(args, "host")?)?;
            launch(state, args, &host, &mut backend)
        }
        "plangraph_package_send" => {
            let run = read(state, string(args, "run")?)?;
            let host = configured_host(state, run["host"].as_str().unwrap())?;
            let text = string(args, "text")?;
            if text.len() > 64 * 1024 {
                return Err(parameter("steering text too large"));
            }
            backend.remote(&host, &crate::package_scripts::send(&host, &run, text))
        }
        "plangraph_package_probe" => crate::package_probe::probe(state, args),
        "plangraph_package_wait" => crate::package_probe::wait(state, args),
        "plangraph_package_list" => {
            Ok(json!({"runs":list(state).into_iter().map(view).collect::<Vec<_>>()}))
        }
        _ => Err(parameter("unknown package tool")),
    }
}
pub(crate) fn cli(args: Vec<String>, out: &mut dyn std::io::Write) -> Result<(), String> {
    if args.len() != 4 || args[0] != "--state" || args[2] != "--tool" {
        return Err("package-call --state <path> --tool <name>".into());
    }
    use std::io::Read;
    let mut input = String::new();
    std::io::stdin()
        .take(1024 * 1024 + 1)
        .read_to_string(&mut input)
        .map_err(|e| e.to_string())?;
    if input.len() > 1024 * 1024 {
        return Err("package input too large".into());
    }
    let params: Args = serde_json::from_str(&input).map_err(|e| e.to_string())?;
    let result = handle(Path::new(&args[1]), &args[3], &params).unwrap_or_else(|e| {
        let error = e.into_error_data();
        json!({"status":"refused","error":error.message})
    });
    writeln!(out, "{result}").map_err(|e| e.to_string())
}
#[cfg(test)]
#[path = "package_tests.rs"]
mod tests;
