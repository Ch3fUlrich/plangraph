use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::{self, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::process::{Command, Output, Stdio};

pub type Env = BTreeMap<OsString, OsString>;
#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct HookOutput {
    #[serde(rename = "exit")]
    pub exit_code: i32,
    pub stdout: String,
    #[serde(default)]
    pub stderr: String,
}
#[derive(Debug)]
pub enum HookError {
    NotImplemented,
    Failed(String),
}
pub trait Hook {
    fn run(&self, event_json: &str, env: &Env) -> Result<HookOutput, HookError>;
}
struct Pending;
impl Hook for Pending {
    fn run(&self, _: &str, _: &Env) -> Result<HookOutput, HookError> {
        Err(HookError::NotImplemented)
    }
}
pub fn registry() -> BTreeMap<&'static str, &'static dyn Hook> {
    [
        "bash_guard",
        "fleet_inject_pre",
        "fleet_inject_post",
        "fleet_report",
        "ctx_guard_session",
    ]
    .into_iter()
    .map(|entry| (entry, &Pending as &dyn Hook))
    .collect()
}
/// Raw input is intentional: malformed JSON is part of the parity contract.
pub fn observe(hook: &dyn Hook, event_json: &str, env: &Env) -> Value {
    match catch_unwind(AssertUnwindSafe(|| hook.run(event_json, env))) {
        Ok(Ok(output)) => json!(output),
        Ok(Err(HookError::NotImplemented)) => json!({"status": "NotImplemented"}),
        Ok(Err(HookError::Failed(error))) => json!({"error": error}),
        Err(_) => json!({"panic": "Rust hook panicked"}),
    }
}
/// Python alone determines the returned bytes and status; shadow logging is best effort.
pub fn shadow(
    entry: &str,
    payload: &[u8],
    env: &Env,
    python: &mut Command,
    hook: &dyn Hook,
    log: &Path,
) -> io::Result<Output> {
    let py = std::thread::scope(|scope| {
        let mut child = python
            .env_clear()
            .envs(env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        let mut stdin = child.stdin.take().unwrap();
        scope.spawn(move || {
            let _ = stdin.write_all(payload);
        });
        child.wait_with_output()
    })?;
    let rust = observe(hook, &String::from_utf8_lossy(payload), env);
    let same = serde_json::from_value::<HookOutput>(rust.clone()).is_ok_and(|r| {
        py.status.code() == Some(r.exit_code)
            && py.stdout == r.stdout.as_bytes()
            && py.stderr == r.stderr.as_bytes()
    });
    let record = json!({"entry": entry, "same": same, "py": {
        "exit": exit_code(&py), "stdout": String::from_utf8_lossy(&py.stdout),
        "stderr": String::from_utf8_lossy(&py.stderr)}, "rust": rust});
    let mut line = record.to_string().into_bytes();
    line.push(b'\n');
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)
    {
        let _ = file.write_all(&line);
    }
    Ok(py)
}
pub fn exit_code(output: &Output) -> i32 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        output
            .status
            .code()
            .unwrap_or(128 + output.status.signal().unwrap_or(0))
    }
    #[cfg(not(unix))]
    {
        output.status.code().unwrap_or(1)
    }
}
