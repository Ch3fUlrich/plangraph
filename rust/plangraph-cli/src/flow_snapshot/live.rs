//! Live readers for the flow snapshot. Each returns JSON for one `Readings` field, or null.
use crate::{ci_watch::github, keeper_events::Client};
use serde_json::{json, Map, Value};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    process::Command,
};

pub fn state_dir(env: &BTreeMap<String, String>) -> Result<PathBuf, String> {
    if let Some(dir) = env.get("PLANGRAPH_STATE_DIR") {
        return Ok(dir.into());
    }
    let home = env
        .get("HOME")
        .cloned()
        .or_else(|| std::env::var("HOME").ok())
        .ok_or("home unavailable")?;
    Ok(PathBuf::from(home).join(".local/state/plangraph"))
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

fn list(env: &BTreeMap<String, String>, key: &str) -> Vec<String> {
    env.get(key)
        .map(|v| {
            v.split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn git(root: &Path, args: &[&str]) -> Result<String, String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!("git {} failed", args.first().unwrap_or(&"")));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_owned())
}

fn root(env: &BTreeMap<String, String>) -> Result<PathBuf, String> {
    match env.get("PLANGRAPH_ROOT") {
        Some(root) => Ok(root.into()),
        None => git(Path::new("."), &["rev-parse", "--show-toplevel"]).map(PathBuf::from),
    }
}

fn used_percent(path: &str) -> Result<f64, String> {
    #[cfg(unix)]
    {
        let c = std::ffi::CString::new(path).map_err(|e| e.to_string())?;
        // SAFETY: statvfs fills a zeroed struct for a valid NUL-terminated path.
        let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c.as_ptr(), &mut stat) } != 0 || stat.f_blocks == 0 {
            return Err(format!("cannot stat {path}"));
        }
        Ok(100.0 * (1.0 - stat.f_bavail as f64 / stat.f_blocks as f64))
    }
    #[cfg(not(unix))]
    Err(format!("disk usage of {path} is unsupported here"))
}

fn usage(root: &Path) -> Result<Value, String> {
    usage_with(root, None)
}

fn usage_with(root: &Path, projects: Option<&Path>) -> Result<Value, String> {
    let mut command = Command::new("python3");
    command
        .arg(root.join("tools/claude_usage.py"))
        .args(["pace", "--json"]);
    if let Some(projects) = projects {
        command.arg("--projects").arg(projects);
    }
    let out = command.output().map_err(|e| e.to_string())?;
    parse_usage(out.status.success(), &out.stdout)
}

/// `claude_usage.py pace --json` prints `{"result": {...}}` on success; on rc 3 it prints
/// `{"status": "no result", ...}`. Only finite numbers from a successful run are a reading.
fn parse_usage(success: bool, stdout: &[u8]) -> Result<Value, String> {
    let body: Value =
        serde_json::from_slice(stdout).map_err(|_| "claude_usage pace gave no JSON")?;
    if !success {
        return Err(format!(
            "claude_usage pace failed: {}",
            body["reason"].as_str().unwrap_or("no result")
        ));
    }
    let number = |name: &str| {
        body["result"][name]
            .as_f64()
            .filter(|n| n.is_finite())
            .ok_or_else(|| format!("claude_usage pace has no finite {name}"))
    };
    Ok(json!({"weighted": number("weighted")?, "budget_per_day": number("budget_per_day")?}))
}

fn ci() -> Result<Value, String> {
    let text = github::command(
        "gh",
        &[
            "run",
            "list",
            "--limit",
            "200",
            "--json",
            "conclusion,createdAt",
        ],
    )?;
    let rows: Vec<Value> = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    Ok(ci_rows(rows))
}

/// Finished runs only; a row without a string conclusion and creation time is not counted.
fn ci_rows(rows: Vec<Value>) -> Value {
    Value::Array(
        rows.into_iter()
            .filter(|r| r["conclusion"].is_string() && r["createdAt"].is_string())
            .map(|r| json!({"conclusion": r["conclusion"], "created": r["createdAt"]}))
            .collect(),
    )
}

fn host_name(env: &BTreeMap<String, String>) -> Result<String, String> {
    env.get("PLANGRAPH_HOST")
        .cloned()
        .map(Ok)
        .unwrap_or_else(|| {
            std::fs::read_to_string("/proc/sys/kernel/hostname")
                .map(|h| h.trim().to_owned())
                .map_err(|_| "host name unavailable".into())
        })
}

/// Refreshes `origin/main` first: a stale ref would under-report the missing commits.
fn host_code(host: &str, root: &Path) -> Result<Value, String> {
    git(root, &["fetch", "--quiet", "origin", "main"])?;
    let head = git(root, &["rev-parse", "HEAD"])?;
    let origin = git(root, &["rev-parse", "origin/main"])?;
    let behind: u64 = git(root, &["rev-list", "--count", "HEAD..origin/main"])?
        .parse()
        .map_err(|_| "bad count")?;
    // Age counts from the first origin/main commit the checkout lacks, not from the checkout's own commit.
    let age = match git(
        root,
        &["log", "--reverse", "--format=%ct", "HEAD..origin/main"],
    )?
    .lines()
    .next()
    {
        Some(first) => (now() - first.parse::<f64>().map_err(|_| "bad time")?).max(0.0) / 3600.0,
        None => 0.0,
    };
    Ok(
        json!([{"host": host, "tmp_used_percent": used_percent("/tmp")?,
               "root_used_percent": used_percent("/")?, "checkout_head": head,
               "origin_main_head": origin, "behind_commits": behind, "age_hours": age}]),
    )
}

fn sha(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// Installed units compared with their source in the checkout. Templated sources (`@NAME@`)
/// differ by construction and are skipped; a unit that is not installed here is not drift.
fn units(host: &str, root: &Path, installed: &Path) -> Result<Value, String> {
    let mut rows = Vec::new();
    for dir in ["deploy/systemd", "deploy/watchdog"] {
        for entry in std::fs::read_dir(root.join(dir))
            .map_err(|e| e.to_string())?
            .flatten()
        {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !(name.ends_with(".service") || name.ends_with(".timer")) || name.contains('@') {
                continue;
            }
            let source = std::fs::read_to_string(entry.path()).map_err(|e| e.to_string())?;
            let target = installed.join(&name);
            if source.contains('@') || !target.is_file() {
                continue;
            }
            rows.push(
                json!({"host": host, "name": name, "deployed_sha": sha(&target)?,
                             "expected_sha": sha(&entry.path())?}),
            );
        }
    }
    Ok(Value::Array(rows))
}

fn series(dir: &Path) -> Result<Value, String> {
    let mut rows = Vec::new();
    for entry in std::fs::read_dir(dir)
        .map_err(|e| format!("{}: {e}", dir.display()))?
        .flatten()
    {
        if entry.path().extension().and_then(|e| e.to_str()) != Some("ndjson") {
            continue;
        }
        let samples =
            pg_worker_core::fleet_series::read(&entry.path()).map_err(|e| e.to_string())?;
        if let Some(last) = samples.last() {
            rows.push(
                json!({"host": last.host, "ts": last.ts, "runs": last.runs, "ready": last.ready,
                             "reachable": last.reachable, "sessions": last.sessions}),
            );
        }
    }
    Ok(Value::Array(rows))
}

/// The sampler's store: `<state>/fleet/<host>.ndjson` (`fleet_series::append`).
fn series_dir(env: &BTreeMap<String, String>) -> Result<PathBuf, String> {
    match env.get("PLANGRAPH_FLEET_SERIES_DIR") {
        Some(dir) => Ok(dir.into()),
        None => state_dir(env).map(|state| state.join("fleet")),
    }
}

/// The stall service's target; its open dependencies are the critical path.
const CRITICAL_TARGET: &str = "plangraph:RT-P1-E2E";

/// Keeper `last_activity` of the claimed and in-review items, the evidence the
/// critical-path stall service reads.
fn with_activity(client: &Client, items: &mut Value) {
    for item in items.as_array_mut().into_iter().flatten() {
        if !matches!(item["state"].as_str(), Some("claimed" | "in_review")) {
            continue;
        }
        let key = item["key"].as_str().unwrap_or_default().to_owned();
        match client.get::<Value>(&format!("v1/show?key={key}&fmt=json")) {
            Ok(shown) => item["last_activity_at"] = shown["last_activity"]["at"].clone(),
            Err(error) => eprintln!("flow-snapshot: show {key} failed: {error}"),
        }
    }
}

/// Keys of Keeper's agent-ready result (`ready?agent=true&all=1`).
fn ready_keys(ready: &Value) -> Value {
    ready["items"]
        .as_array()
        .map(|rows| Value::Array(rows.iter().map(|r| r["key"].clone()).collect()))
        .unwrap_or(Value::Null)
}

pub fn collect(env: &BTreeMap<String, String>) -> Value {
    let mut out = Map::new();
    out.insert("now".into(), json!(now()));
    out.insert(
        "coding_hosts".into(),
        json!(list(env, "PLANGRAPH_FLOW_CODING_HOSTS")),
    );
    out.insert(
        "worker_hosts".into(),
        json!(list(env, "PLANGRAPH_FLOW_WORKER_HOSTS")),
    );
    let mut put = |name: &str, result: Result<Value, String>| {
        out.insert(
            name.into(),
            result.unwrap_or_else(|error| {
                eprintln!("flow-snapshot: {name} reading failed: {error}");
                Value::Null
            }),
        );
    };
    let root = root(env);
    let at = |f: &dyn Fn(&Path) -> Result<Value, String>| {
        root.as_ref().map_err(Clone::clone).and_then(|r| f(r))
    };
    put("usage", at(&usage));
    let host = host_name(env);
    put(
        "hosts",
        host.clone().and_then(|h| at(&|r| host_code(&h, r))),
    );
    let installed = pg_worker_core::unit_refs::systemd_user_dir().map_err(|e| e.to_string());
    put(
        "units",
        host.and_then(|h| installed.and_then(|dir| at(&|r| units(&h, r, &dir)))),
    );
    put("ci", ci());
    put("series", series_dir(env).and_then(|d| series(&d)));
    let keeper = Client::new(env).and_then(|client| {
        let mut snapshot: Value = client.get("v1/snapshot")?;
        with_activity(&client, &mut snapshot["items"]);
        let ready: Value = client.get("v1/ready?agent=true&all=1")?;
        Ok((snapshot, ready))
    });
    put("critical_target", Ok(json!(CRITICAL_TARGET)));
    match keeper {
        Ok((snapshot, ready)) => {
            put("items", Ok(snapshot["items"].clone()));
            put("activity", Ok(snapshot["activity"].clone()));
            put("ready_keys", Ok(ready_keys(&ready)));
        }
        Err(error) => {
            for name in ["items", "activity", "ready_keys"] {
                put(name, Err(error.clone()));
            }
        }
    }
    Value::Object(out)
}

#[cfg(test)]
#[path = "live_tests.rs"]
mod tests;
