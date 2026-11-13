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
#[derive(Deserialize)]
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
pub(crate) fn prompt_carrier(launch: &LaunchSpec, prompt: &str) -> bool {
    launch
        .argv
        .iter()
        .chain(launch.env.values())
        .any(|value| value == prompt || (prompt.len() >= 16 && value.contains(prompt)))
        || stdin_metadata_carrier(&launch.argv, prompt)
}
fn stdin_metadata_carrier(argv: &[String], prompt: &str) -> bool {
    // Only actual stdin modes have task-bearing flag values. Keep incidental
    // substrings in legacy/non-launch metadata distinct from task transport.
    let pair = |flag: &str, value: &str| argv.windows(2).any(|w| w == [flag, value]);
    let binary = argv
        .first()
        .and_then(|s| Path::new(s).file_stem())
        .and_then(|s| s.to_str());
    let known = match binary {
        Some("opencode") => argv.get(1).is_some_and(|s| s == "run"),
        Some("codex") => argv.get(1).is_some_and(|s| s == "exec") && argv.iter().any(|s| s == "-"),
        Some("agy") => pair("-p", "") && pair("--input-format", "stream-json"),
        Some("qodercli") => pair("-p", "") && pair("--input-format", "text"),
        Some("claude") => argv.iter().any(|s| s == "-p") && !argv.iter().any(|s| s == "--bg"),
        Some("gemini" | "qwen") => pair("-p", ""),
        _ => false,
    };
    known
        && argv.iter().enumerate().any(|(i, arg)| {
            let (flag, value) = arg
                .split_once('=')
                .unwrap_or((arg, argv.get(i + 1).map(String::as_str).unwrap_or("")));
            matches!(flag, "--title" | "--model")
                && value.contains(prompt)
                // Match Python: short metadata prompts must occupy whole text tokens.
                && (prompt.len() >= 16
                    || value.match_indices(prompt).any(|(start, _)| {
                        let before = &value[..start];
                        let after = &value[start + prompt.len()..];
                        (before.is_empty() || before.ends_with(char::is_whitespace))
                            && (after.is_empty() || after.starts_with(char::is_whitespace))
                    }))
                && !(flag == "--title"
                    && "plangraph-job-".contains(prompt)
                    && value.strip_prefix("plangraph-job-").is_some_and(|id| {
                        id.len() == 8 && id.bytes().all(|b| b.is_ascii_hexdigit())
                    }))
        })
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
        let mut child = cmd
            .spawn()
            .map_err(|_| internal("launch adapter could not start"))?;
        let mut stdin = child.stdin.take().unwrap();
        let bytes = serde_json::to_vec(request).map_err(|_| invalid())?;
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
