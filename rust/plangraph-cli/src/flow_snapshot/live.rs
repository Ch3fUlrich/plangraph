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
    let out = Command::new("python3")
        .arg(root.join("tools/claude_usage.py"))
        .args(["pace", "--json"])
        .output()
        .map_err(|e| e.to_string())?;
    let body: Value =
        serde_json::from_slice(&out.stdout).map_err(|_| "claude_usage pace gave no JSON")?;
    Ok(json!({"weighted": body["weighted"], "budget_per_day": body["budget_per_day"]}))
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
    Ok(Value::Array(
        rows.into_iter()
            .filter(|r| r["conclusion"].is_string())
            .map(|r| json!({"conclusion": r["conclusion"], "created": r["createdAt"]}))
            .collect(),
    ))
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

fn host_code(env: &BTreeMap<String, String>, root: &Path) -> Result<Value, String> {
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
        json!([{"host": host_name(env)?, "tmp_used_percent": used_percent("/tmp")?,
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
fn units(env: &BTreeMap<String, String>, root: &Path) -> Result<Value, String> {
    let installed = pg_worker_core::unit_refs::systemd_user_dir().map_err(|e| e.to_string())?;
    let host = host_name(env)?;
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
    put("hosts", at(&|r| host_code(env, r)));
    put("units", at(&|r| units(env, r)));
    put("ci", ci());
    let dir = env
        .get("PLANGRAPH_FLEET_SERIES_DIR")
        .map(PathBuf::from)
        .map_or_else(|| state_dir(env).map(|s| s.join("fleet-series")), Ok);
    put("series", dir.and_then(|d| series(&d)));
    let keeper = Client::new(env).and_then(|client| {
        let snapshot: Value = client.get("v1/snapshot")?;
        let path: Value = client.get("v1/critical_path")?;
        Ok((snapshot, path))
    });
    match keeper {
        Ok((snapshot, path)) => {
            put("items", Ok(snapshot["items"].clone()));
            put("activity", Ok(snapshot["activity"].clone()));
            put("critical_path", Ok(path["keys"].clone()));
        }
        Err(error) => {
            for name in ["items", "activity", "critical_path"] {
                put(name, Err(error.clone()));
            }
        }
    }
    Value::Object(out)
}
