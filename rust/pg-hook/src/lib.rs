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
    fn run(&self, event_json: &[u8], env: &Env) -> Result<HookOutput, HookError>;
}
struct Pending;
impl Hook for Pending {
    fn run(&self, _: &[u8], _: &Env) -> Result<HookOutput, HookError> {
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
/// Raw bytes are intentional: malformed JSON and non-UTF-8 are parity inputs too.
pub fn observe(hook: &dyn Hook, event_json: &[u8], env: &Env) -> Value {
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
    let rust = observe(hook, payload, env);
    let py = std::thread::scope(|scope| {
        let mut child = python
            .env_clear()
            .envs(env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| io::Error::other("Python spawn failed"))?;
        let mut stdin = child.stdin.take().unwrap(); // Piped above, never inherited.
        let writer = scope.spawn(move || stdin.write_all(payload));
        // Drain outputs concurrently with stdin to avoid pipe-capacity deadlocks.
        let output = child.wait_with_output();
        let fault = match writer.join() {
            Ok(Ok(())) => None,
            Ok(Err(_)) => Some("Python stdin write failed"),
            Err(_) => Some("Python stdin writer panicked"),
        };
        output
            .map(|output| (output, fault))
            .map_err(|_| io::Error::other("Python wait failed"))
    });
    let (python, same, fault) = match &py {
        Ok((output, fault)) => {
            let exit = comparison_exit_code(output);
            let same = fault.is_none().then_some(()).and_then(|()| {
                let rust = serde_json::from_value::<HookOutput>(rust.clone()).ok()?;
                Some(
                    exit? == rust.exit_code
                        && output.stdout == rust.stdout.as_bytes()
                        && output.stderr == rust.stderr.as_bytes(),
                )
            });
            (
                json!({"exit": exit, "stdout": String::from_utf8_lossy(&output.stdout),
                "stderr": String::from_utf8_lossy(&output.stderr)}),
                same,
                fault.map(str::to_owned),
            )
        }
        Err(error) => (Value::Null, None, Some(error.to_string())),
    };
    let mut record = json!({"entry": entry, "same": same, "py": python, "rust": rust});
    if let Some(error) = fault {
        record["harness"] = json!({"error": error});
    }
    append_record(log, &record);
    py.map(|(output, _)| output)
}

/// Missing input or a panicking harness is an observation, not a parity mismatch.
pub fn record_harness_fault(entry: &str, log: &Path, error: &str) {
    append_record(
        log,
        &json!({"entry": entry, "same": null, "py": null, "rust": null,
        "harness": {"error": error}}),
    );
}

fn append_record(log: &Path, record: &Value) {
    let mut line = record.to_string().into_bytes();
    line.push(b'\n');
    let mut options = std::fs::OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    if let Ok(mut file) = options.open(log) {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            // Also tighten older logs before appending potentially private outputs.
            if file
                .set_permissions(std::fs::Permissions::from_mode(0o600))
                .is_err()
            {
                return;
            }
        }
        let _ = file.write_all(&line);
    }
}
pub fn exit_code(output: &Output) -> i32 {
    // A status the platform cannot represent must fail open, not invent a failure.
    comparison_exit_code(output).unwrap_or(0)
}

fn comparison_exit_code(output: &Output) -> Option<i32> {
    #[cfg(unix)]
    let signal = {
        use std::os::unix::process::ExitStatusExt;
        output.status.signal()
    };
    #[cfg(not(unix))]
    let signal = None;
    normalized_exit_code(output.status.code(), signal)
}

fn normalized_exit_code(code: Option<i32>, signal: Option<i32>) -> Option<i32> {
    code.or_else(|| signal.map(|signal| 128 + signal))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unavailable_platform_status_is_not_a_synthetic_failure() {
        assert_eq!(normalized_exit_code(Some(0), None), Some(0));
        assert_eq!(normalized_exit_code(Some(1), None), Some(1));
        assert_eq!(normalized_exit_code(Some(7), None), Some(7));
        assert_eq!(normalized_exit_code(None, Some(15)), Some(143));
        assert_eq!(normalized_exit_code(None, None), None);
    }

    #[cfg(unix)]
    #[test]
    fn unavailable_exit_code_fails_open_without_a_comparison() {
        use std::os::unix::process::ExitStatusExt;
        let output = Output {
            // A stopped status has neither a completed exit code nor a death signal.
            status: std::process::ExitStatus::from_raw(0x7f),
            stdout: Vec::new(),
            stderr: Vec::new(),
        };
        assert_eq!(comparison_exit_code(&output), None);
        assert_eq!(exit_code(&output), 0);
    }
}
