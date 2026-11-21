//! Operator-owned harness construction. Environments cross pipes only, never records.
#![allow(dead_code)] // Wired into spawn in the third stacked commit.
use crate::errors::{RefusalKind, ToolError};
use serde::Deserialize;
use serde_json::Value;
use std::{
    collections::BTreeMap,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub(crate) struct LaunchAdapter(Option<std::ffi::OsString>, std::ffi::OsString);
#[cfg(all(test, unix))]
#[path = "launch_tests.rs"]
mod tests;
#[derive(Clone, Deserialize)]
pub(crate) struct LaunchSpec {
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    pub env: BTreeMap<String, String>,
    pub stdin: Option<String>,
    #[serde(default)]
    pub prompt: Option<String>,
    pub warnings: Option<Vec<String>>,
    pub events: Option<Vec<serde_json::Map<String, Value>>>,
}
fn internal(message: &str) -> ToolError {
    ToolError::Internal(message.into())
}
fn invalid() -> ToolError {
    internal("launch adapter returned an invalid launch spec")
}
fn parse(bytes: &[u8]) -> Result<LaunchSpec, ToolError> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| invalid())?;
    if !value
        .get("stdin")
        .is_some_and(|v| v.is_null() || v.is_string())
    {
        return Err(invalid());
    }
    let spec: LaunchSpec = serde_json::from_value(value).map_err(|_| invalid())?;
    if spec.argv.is_empty()
        || spec.argv.len() > 1024
        || spec.argv[0].is_empty()
        || spec.argv[0].starts_with('-')
        || spec.argv.iter().any(|s| s.contains('\0'))
        || !spec.cwd.is_dir()
        || spec.env.len() > 1024
        || spec
            .env
            .iter()
            .any(|(k, v)| k.is_empty() || k.contains(['=', '\0']) || v.contains('\0'))
        || spec
            .stdin
            .as_ref()
            .is_some_and(|s| s.len() > 8 * 1024 * 1024)
        || spec.events.as_ref().is_some_and(|events| {
            events
                .iter()
                .any(|e| !e.get("event").is_some_and(Value::is_string))
        })
    {
        return Err(invalid());
    }
    Ok(spec)
}
/// A review of a registered-repository `diff_ref` also excludes the families named by the head's
/// `Writer` trailers: the Python launch adapter cannot resolve `repo_id`, so the families join
/// `writer_families` here. A head that cannot be read refuses; refs the later admission rejects pass through.
pub(crate) type RepoMap = std::collections::BTreeMap<String, pg_worker_core::hosts::RepoEntry>;

pub(crate) fn with_head_writers(request: &Value, repos: &RepoMap) -> Result<Value, ToolError> {
    let mut request = request.clone();
    let reference = &request["card"]["diff_ref"];
    if request["role"] != "review" || reference.get("repo_id").is_none() {
        return Ok(request);
    }
    // The Python adapter cannot resolve a registered ref, so an unresolved one must not launch
    // without the head writer's exclusion. The malformed value is never echoed.
    let refuse = |message: &str| ToolError::Refused {
        kind: crate::errors::RefusalKind::Spawn,
        message: message.to_owned(),
        legs: vec![],
    };
    let diff = serde_json::from_value::<pg_worker_core::workspace::DiffRef>(reference.clone())
        .map_err(|_| refuse("invalid registered-repository diff_ref"))?;
    let repo = repos
        .get(&diff.repo_id)
        .ok_or_else(|| refuse("unknown registered repository in diff_ref"))?;
    let writers = crate::prompt_git::head_writers(repo, &diff)
        .map_err(|_| refuse("cannot read the Writer trailer of the registered head"))?;
    let mut families = request["writer_families"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    for writer in writers {
        if !families.iter().any(|f| f == &Value::from(writer.as_str())) {
            families.push(Value::from(writer));
        }
    }
    request["writer_families"] = Value::Array(families);
    Ok(request)
}
fn drain(mut reader: impl Read, cap: usize) -> Vec<u8> {
    let mut kept = Vec::new();
    let mut buf = [0; 8192];
    while let Ok(n) = reader.read(&mut buf) {
        if n == 0 {
            break;
        }
        kept.extend_from_slice(&buf[..n.min(cap.saturating_sub(kept.len()))]);
    }
    kept
}
impl LaunchAdapter {
    pub(crate) fn from_env() -> Self {
        Self::from_lookup(&|key| std::env::var_os(key))
    }
    fn from_lookup(lookup: &dyn Fn(&str) -> Option<std::ffi::OsString>) -> Self {
        Self(
            lookup("PG_SPAWND_LAUNCH_CMD").filter(|s| !s.is_empty()),
            lookup("PG_SPAWND_PYTHON")
                .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into()),
        )
    }
    pub(crate) fn request(
        &self,
        state: &Path,
        request: &Value,
        timeout: Duration,
    ) -> Result<LaunchSpec, ToolError> {
        // Resolve the head writers and serialise before any child exists: an error here leaves nothing to reap.
        let request = with_head_writers(request, &crate::prompt::repos())?;
        let bytes = serde_json::to_vec(&request).map_err(|_| invalid())?;
        let mut cmd = if let Some(program) = &self.0 {
            if !Path::new(program).is_absolute() {
                return Err(internal("launch adapter could not start"));
            }
            Command::new(program)
        } else {
            let mut cmd = Command::new(&self.1);
            cmd.args(["-m", "plangraph.spawner.cli", "--state"])
                .arg(state)
                .arg("launch-spec");
            cmd
        };
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
        }
        let mut child = pg_worker_core::stall::spawn_retrying(&mut cmd)
            .map_err(|e| internal(&format!("launch adapter could not start: {e}")))?;
        let mut stdin = child.stdin.take().unwrap();
        std::thread::spawn(move || {
            let _ = stdin.write_all(&bytes);
        });
        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        let out = tx.clone();
        std::thread::spawn(move || {
            let _ = out.send((true, drain(stdout, 16 * 1024 * 1024 + 1)));
        });
        std::thread::spawn(move || {
            let _ = tx.send((false, drain(stderr, 8192)));
        });
        let start = Instant::now();
        let status = loop {
            if let Ok(Some(status)) = child.try_wait() {
                break Some(status);
            }
            if start.elapsed() >= timeout {
                #[cfg(unix)]
                unsafe {
                    libc::killpg(child.id() as i32, libc::SIGKILL);
                }
                #[cfg(windows)]
                let _ = child.kill();
                let _ = child.wait();
                break None;
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        // Descendants must not keep the output pipes open after the adapter exits.
        #[cfg(unix)]
        unsafe {
            libc::killpg(child.id() as i32, libc::SIGKILL);
        }
        let deadline = Instant::now() + Duration::from_secs(1);
        let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
        for _ in 0..2 {
            let (is_out, bytes) = rx
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .map_err(|_| internal("launch adapter timed out"))?;
            if is_out {
                stdout = bytes;
            } else {
                stderr = bytes;
            }
        }
        let status = status.ok_or_else(|| internal("launch adapter timed out"))?;
        let code = status
            .code()
            .ok_or_else(|| internal("launch adapter killed by a signal"))?;
        match code {
            0 if stdout.len() <= 16 * 1024 * 1024 => parse(&stdout),
            0 => Err(invalid()),
            2 => {
                let text = String::from_utf8_lossy(&stderr);
                let line = text
                    .lines()
                    .next()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("launch refused");
                Err(ToolError::Refused {
                    kind: RefusalKind::Spawn,
                    message: pg_worker_core::redact::redact(line)
                        .chars()
                        .take(300)
                        .collect(),
                    legs: vec![],
                })
            }
            n => Err(internal(&format!("launch adapter failed (exit {n})"))),
        }
    }
}
