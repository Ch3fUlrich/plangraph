//! Private, bounded SSH source probes. Transport diagnostics are never captured.
use regex::Regex;
use serde_json::{json, Value};
use std::{
    io,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

const MAX_WORKERS: usize = 16;
const TIMEOUT: Duration = Duration::from_secs(3);

fn row(name: &str, detail: &str) -> Value {
    json!({"name": name, "status": if detail == "ok" { "pass" } else { "fail" }, "detail": detail})
}

fn matches(pattern: &str, value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|s| Regex::new(pattern).unwrap().is_match(s))
}

fn valid(h: &Value) -> bool {
    matches(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\z", &h["name"])
        && matches(r"\A[A-Za-z0-9_@.:-]+\z", &h["target"])
        && !h["target"].as_str().unwrap().starts_with('-')
        && !h["invalid"].as_bool().unwrap_or(false)
        && (h.get("src").is_some() || h.get("win_src").is_some())
}

fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}

fn command(h: &Value) -> Option<String> {
    if let Some(src) = h.get("win_src") {
        return matches(r"\A[A-Za-z]:[/\\][A-Za-z0-9_ ./\\-]*\z", src)
            .then(|| format!("if not exist \"{}\\\" exit /b 44", src.as_str().unwrap()));
    }
    let src = h["src"].as_str()?;
    let path = src
        .strip_prefix("~/")
        .map(|s| format!("\"$HOME\"/{}", quote(s)))
        .unwrap_or_else(|| quote(src));
    let probe = format!("test -d {path} || exit 44");
    if let Some(distro) = h.get("wsl_distro") {
        if !matches(r"\A[A-Za-z0-9_.][A-Za-z0-9_.-]*\z", distro) {
            return None;
        }
        Some(format!(
            "wsl -d {} -- sh -c {}",
            distro.as_str()?,
            quote(&probe)
        ))
    } else {
        Some(probe)
    }
}

fn ssh(target: &str, command: &str) -> io::Result<Option<i32>> {
    let mut child = Command::new("ssh")
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=2",
            "--",
            target,
            command,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(Some(status.code().unwrap_or(-1))),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(5)),
            result => {
                let _ = child.kill();
                let _ = child.wait();
                return result.map(|_| None);
            }
        }
    }
}

fn check(h: &Value, run: &impl Fn(&str, &str) -> io::Result<Option<i32>>) -> Value {
    let detail = match command(h).zip(h["target"].as_str()) {
        None => "invalid configuration",
        Some((cmd, target)) => match run(target, &cmd) {
            Ok(Some(0)) => "ok",
            Ok(Some(44)) => "src missing",
            Ok(Some(255)) | Err(_) => "unreachable",
            _ => "host query failed",
        },
    };
    row(&format!("ps_host_{}", h["name"].as_str().unwrap()), detail)
}

fn source_checks(
    data: io::Result<String>,
    run: &(impl Fn(&str, &str) -> io::Result<Option<i32>> + Sync),
) -> Vec<Value> {
    let data = match data {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return vec![],
        other => other
            .ok()
            .and_then(|s| serde_json::from_str::<Value>(&s).ok()),
    };
    let Some(hosts) = data.as_ref().and_then(|d| d["hosts"].as_array()) else {
        return vec![row("ps_hosts", "invalid configuration")];
    };
    let hosts: Vec<_> = hosts.iter().filter(|h| valid(h)).collect();
    if hosts.is_empty() {
        return vec![];
    }
    let next = std::sync::atomic::AtomicUsize::new(0);
    let results: std::sync::Mutex<Vec<(usize, Value)>> = Default::default();
    std::thread::scope(|scope| {
        for _ in 0..MAX_WORKERS.min(hosts.len()) {
            scope.spawn(|| loop {
                let i = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let Some(h) = hosts.get(i) else {
                    break;
                };
                let result = check(h, run);
                results.lock().unwrap().push((i, result));
            });
        }
    });
    let mut results = results.into_inner().unwrap();
    results.sort_by_key(|r| r.0);
    results.into_iter().map(|r| r.1).collect()
}

pub(crate) fn run() -> Vec<Value> {
    let root = std::env::var_os("XDG_CONFIG_HOME")
        .filter(|s| !s.is_empty())
        .map(Into::into)
        .unwrap_or_else(|| {
            Path::new(&std::env::var_os("HOME").unwrap_or_default()).join(".config")
        });
    source_checks(
        std::fs::read_to_string(root.join("plangraph/ps-hosts.json")),
        &ssh,
    )
}

#[cfg(test)]
#[path = "doctor_sources_tests.rs"]
mod tests;
