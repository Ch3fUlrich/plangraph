//! Bounded command transport; tests inject Backend and never SSH to real hosts.
use super::package::{Backend, Host};
use crate::errors::ToolError;
use serde_json::Value;
use std::{
    io::{Read, Write},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub(crate) struct Native;
fn error(message: &str) -> ToolError {
    ToolError::Internal(message.into())
}
fn capture(command: &mut Command, input: &str) -> Result<(i32, String, String), ToolError> {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .map_err(|_| error("package command could not start"))?;
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let input = input.as_bytes().to_vec();
    std::thread::spawn(move || {
        let _ = stdin.write_all(&input);
    });
    let (send, recv) = std::sync::mpsc::channel();
    for (out, mut pipe) in [
        (true, Box::new(stdout) as Box<dyn Read + Send>),
        (false, Box::new(stderr)),
    ] {
        let send = send.clone();
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let mut buf = [0; 8192];
            while let Ok(n) = pipe.read(&mut buf) {
                if n == 0 {
                    break;
                }
                bytes.extend_from_slice(
                    &buf[..n.min((1024 * 1024usize).saturating_sub(bytes.len()))],
                );
            }
            let _ = send.send((out, String::from_utf8_lossy(&bytes).into_owned()));
        });
    }
    let start = Instant::now();
    let status = loop {
        if let Ok(Some(status)) = child.try_wait() {
            break status;
        }
        if start.elapsed() > Duration::from_secs(120) {
            #[cfg(unix)]
            unsafe {
                libc::killpg(child.id() as i32, libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
            return Err(error("package command timed out"));
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let mut out = String::new();
    let mut err = String::new();
    for _ in 0..2 {
        let (is_out, text) = recv
            .recv_timeout(Duration::from_secs(2))
            .map_err(|_| error("package capture timed out"))?;
        if is_out {
            out = text;
        } else {
            err = text;
        }
    }
    Ok((status.code().unwrap_or(-1), out, err))
}
impl Backend for Native {
    fn keeper(&mut self, verb: &str, key: &str) -> Result<Value, ToolError> {
        let mut command =
            Command::new(std::env::var_os("PG_SPAWND_PYTHON").unwrap_or_else(|| "python3".into()));
        command.args(["-m", "plangraph.keeper.cli", verb, key, "--json"]);
        let (code, out, err) = capture(&mut command, "")?;
        if code == 0 {
            serde_json::from_str(&out).map_err(|_| error("invalid keeper reply"))
        } else if code == 2 {
            Ok(
                serde_json::json!({"status":"refused","reason":pg_worker_core::redact::redact(err.trim())}),
            )
        } else {
            Err(error("keeper unavailable; package launch refused"))
        }
    }
    fn remote(&mut self, host: &Host, script: &str) -> Result<Value, ToolError> {
        if host.target.starts_with('-') || host.target.chars().any(char::is_whitespace) {
            return Err(error("invalid configured SSH target"));
        }
        let mut command = Command::new("ssh");
        command.args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            &host.target,
        ]);
        if host.kind == "windows" {
            command.arg("powershell -NoProfile -NonInteractive -Command -");
        } else {
            command.arg("sh -s");
        }
        let (code, out, _) = capture(&mut command, script)?;
        if code != 0 {
            return Err(error("remote package command failed; inspect package run"));
        }
        // herdr/git may print diagnostics before the final JSON object.
        out.lines()
            .rev()
            .find_map(|line| serde_json::from_str(line).ok())
            .ok_or_else(|| error("remote package command returned no JSON"))
    }
}
