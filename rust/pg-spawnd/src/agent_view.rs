//! File-only local inventory and bounded SSH collection. Never starts agent runtimes.
use super::agent_view_projection::{project, project_view, unavailable};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
fn read(path: &Path) -> Result<Value, String> {
    let value: Value = serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    if !value.is_object() {
        return Err("expected object".into());
    }
    Ok(value)
}
fn children(path: &Path) -> Result<Vec<PathBuf>, String> {
    match fs::read_dir(path) {
        Ok(entries) => entries
            .map(|e| e.map(|e| e.path()).map_err(|e| e.to_string()))
            .collect(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(vec![]),
        Err(e) => Err(e.to_string()),
    }
}
fn local(home: &Path, state: &Path, host: &str, now: f64) -> Value {
    let (mut agents, mut failures) = (vec![], 0);
    let mut identities = std::collections::HashSet::new();
    let mut ingest = |path: PathBuf, runtime: &str| match read(&path) {
        Ok(source) => {
            let row = project(&source, host, Some(runtime), Some(now));
            let ended = source
                .get("ended")
                .is_some_and(|v| !v.is_null() && v != false && v != 0 && v != "");
            if !source["stopped_ts"].is_null()
                || ended
                || [
                    "done",
                    "stopped",
                    "completed",
                    "failed",
                    "cancelled",
                    "exited",
                ]
                .contains(&row["state"].as_str().unwrap_or(""))
            {
                return;
            }
            let identity = source["run_id"]
                .as_str()
                .map(|id| (Some(id.to_owned()), None))
                .unwrap_or_else(|| (None, Some(path.clone())));
            if identities.insert(identity) {
                agents.push(row);
            }
        }
        Err(_) => {
            failures += 1;
        }
    };
    let mut source_errors = 0;
    let mut list = |path: &Path| {
        children(path).unwrap_or_else(|_| {
            source_errors += 1;
            vec![]
        })
    };
    list(state);
    for path in list(&state.join("runs")) {
        let record = path.join("record.json");
        if record.try_exists().unwrap_or(true) {
            ingest(record, "spawner-run");
        }
    }
    for path in list(&state.join("sessions")) {
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if name.ends_with(".json") && name != "_counters.json" && !name.ends_with(".ready.json") {
            ingest(path, "spawner-run");
        }
    }
    for path in list(&home.join(".claude/jobs")) {
        let record = path.join("state.json");
        if record.try_exists().unwrap_or(true) {
            ingest(record, "claude");
        }
    }
    let scan = || -> Result<(), String> {
        let config = home.join(".config/autoos/oc-l1.json");
        if config.try_exists().map_err(|e| e.to_string())? {
            let config = read(&config)?;
            let lanes = config.get("lanes").unwrap_or(&config);
            let lanes: Vec<_> = match lanes {
                Value::Object(m) => m.values().collect(),
                Value::Array(a) => a.iter().collect(),
                _ => return Err("bad lanes".into()),
            };
            for lane in lanes {
                if !lane.is_object() {
                    source_errors += 1;
                    continue;
                }
                if let Some(s) = lane["state_file"].as_str() {
                    let path = s
                        .strip_prefix("~/")
                        .map(|s| home.join(s))
                        .unwrap_or_else(|| s.into());
                    ingest(path, "opencode");
                }
            }
        }
        Ok(())
    };
    let mut scan = scan;
    if scan().is_err() {
        failures += 1;
    }
    if failures + source_errors > 0 {
        json!({"host":host,"readable":false,"reason":"source_unreadable","agents":agents})
    } else {
        json!({"host":host,"readable":true,"reason":null,"agents":agents})
    }
}
fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}
fn response(host: &str, bytes: &[u8]) -> Value {
    let parsed = serde_json::from_slice::<Value>(bytes);
    let Some(entry) = parsed.as_ref().ok().and_then(|v| v["hosts"].get(0)) else {
        return unavailable(host, "invalid_response");
    };
    if !entry.is_object() {
        return unavailable(host, "invalid_response");
    }
    if entry["readable"] != true {
        return unavailable(host, "source_unreadable");
    }
    let Some(rows) = entry["agents"]
        .as_array()
        .filter(|r| r.iter().all(Value::is_object))
    else {
        return unavailable(host, "invalid_response");
    };
    json!({"host":host,"readable":true,"reason":null,
        "agents":rows.iter().map(|r| project(r,host,None,None)).collect::<Vec<_>>()})
}
fn remote(host: &str, config: &Value, timeout: Duration) -> Value {
    let Some(ssh) = config["ssh"].as_str().filter(|s| !s.is_empty()) else {
        return unavailable(host, "no_route");
    };
    let Some(src) = config["spawner_src"].as_str() else {
        return unavailable(host, "invalid_response");
    };
    let mut command = format!("cd {} && ", quote(src));
    if let Some(env) = config["env_file"].as_str() {
        command += &format!("set -a && . {} && set +a && ", quote(env));
    }
    command += "pg-spawnd agents-view --json --local";
    let Ok(mut child) = Command::new("ssh")
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            ssh,
            &command,
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return unavailable(host, "invalid_response");
    };
    let mut stdout = child.stdout.take().expect("piped stdout");
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut bytes = vec![];
        let result = stdout.read_to_end(&mut bytes);
        let _ = tx.send((result, bytes));
    });
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return unavailable(host, "ssh_failed");
                }
                return match rx.recv_timeout(timeout.saturating_sub(start.elapsed())) {
                    Ok((Ok(_), bytes)) => response(host, &bytes),
                    _ => unavailable(host, "timeout"),
                };
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return unavailable(host, "invalid_response");
            }
            _ if start.elapsed() >= timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return unavailable(host, "timeout");
            }
            _ => thread::sleep(Duration::from_millis(5)),
        }
    }
}
fn gather(
    entries: &serde_json::Map<String, Value>,
    timeout: Duration,
    run: impl Fn(&str, &Value, Duration) -> Value + Send + Sync + 'static,
) -> Vec<Value> {
    let run = std::sync::Arc::new(run);
    let deadline = Instant::now() + timeout + Duration::from_millis(250);
    let receivers: Vec<_> = entries
        .iter()
        .map(|(host, cfg)| {
            let (tx, rx) = mpsc::channel();
            let (name, cfg, run) = (host.clone(), cfg.clone(), run.clone());
            thread::spawn(move || {
                let _ = tx.send(run(&name, &cfg, timeout));
            });
            (host, rx)
        })
        .collect();
    receivers
        .into_iter()
        .map(|(host, rx)| {
            rx.recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .unwrap_or_else(|_| unavailable(host, "timeout"))
        })
        .collect()
}
pub(crate) fn execute(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if args.first().is_some_and(|s| s == "--help" || s == "-h") {
        return writeln!(
            out,
            "Usage: pg-spawnd agents-view [--json] [--write] [--local]"
        )
        .map_err(|e| e.to_string());
    }
    let mut seen = std::collections::HashSet::new();
    for arg in &args {
        if !["--json", "--write", "--local"].contains(&arg.as_str()) || !seen.insert(arg.as_str()) {
            return Err("unknown or duplicate flag".into());
        }
    }
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or("missing home")?;
    let state = crate::state::resolve_state(None, |key| std::env::var_os(key))?;
    let host = std::env::var("HOSTNAME")
        .or_else(|_| fs::read_to_string("/etc/hostname").map(|s| s.trim().to_owned()))
        .or_else(|_| std::env::var("COMPUTERNAME"))
        .unwrap_or_else(|_| "unknown".into());
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_secs_f64();
    let mut hosts = vec![local(&home, &state, &host, now)];
    let config = std::env::var_os("PLANGRAPH_OVERFLOW_HOSTS")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config/plangraph/overflow-hosts.json"));
    if !seen.contains("--local") && config.exists() {
        match read(&config)
            .ok()
            .and_then(|v| v["hosts"].as_object().cloned())
        {
            Some(entries) => hosts.extend(gather(&entries, Duration::from_secs(20), remote)),
            None => hosts.push(unavailable("unknown", "config_unreadable")),
        }
    }
    // ISO UTC without an additional clock dependency (milliseconds are unnecessary).
    let secs = now as u64;
    let days = secs / 86400;
    let mut year = 1970;
    let mut day = days;
    let leap = |y| y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    while day >= if leap(year) { 366 } else { 365 } {
        day -= if leap(year) { 366 } else { 365 };
        year += 1;
    }
    let mut month = 1;
    for length in [
        31,
        if leap(year) { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ] {
        if day < length {
            break;
        }
        day -= length;
        month += 1;
    }
    let stamp = format!(
        "{year:04}-{month:02}-{:02}T{:02}:{:02}:{:02}Z",
        day + 1,
        secs / 3600 % 24,
        secs / 60 % 60,
        secs % 60
    );
    let data = json!({"generated_at":stamp,"hosts":hosts});
    if seen.contains("--write") {
        fs::create_dir_all(home.join(".local/state/plangraph")).map_err(|e| e.to_string())?;
        pg_worker_core::run::record_io::atomic_write_private(
            &home.join(".local/state/plangraph/agents-view.json"),
            project_view(&data).to_string().as_bytes(),
        )
        .map_err(|e| e.to_string())?;
    }
    writeln!(
        out,
        "{}",
        if seen.contains("--json") {
            data.to_string()
        } else {
            serde_json::to_string_pretty(&data).map_err(|e| e.to_string())?
        }
    )
    .map_err(|e| e.to_string())
}
#[cfg(test)]
#[path = "agent_view_tests.rs"]
mod tests;
