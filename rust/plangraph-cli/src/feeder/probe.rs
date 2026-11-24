//! Bounded external reads: a child's stdout, killed at its deadline.
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Run `cmd` to completion and return its stdout; kill it and fail after `timeout`.
pub(super) fn capture(cmd: &mut Command, timeout: Duration) -> Result<Vec<u8>, String> {
    let name = cmd.get_program().to_string_lossy().into_owned();
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("cannot start {name}: {e}"))?;
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(Some(status)) => return Err(format!("{name} exited with {status}")),
            Ok(None) if start.elapsed() > timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("{name} timed out after {}s", timeout.as_secs()));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(format!("{name} wait failed: {e}")),
        }
    }
    child
        .wait_with_output()
        .map(|out| out.stdout)
        .map_err(|e| format!("{name} output: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_returns_stdout_and_kills_a_child_past_its_deadline() {
        let out = capture(Command::new("echo").arg("hi"), Duration::from_secs(5)).unwrap();
        assert_eq!(out, b"hi\n");
        let err = capture(Command::new("sleep").arg("5"), Duration::from_millis(200)).unwrap_err();
        assert!(err.starts_with("sleep timed out"), "{err}");
        assert!(capture(&mut Command::new("false"), Duration::from_secs(5)).is_err());
        assert!(capture(&mut Command::new("/nonexistent/x"), Duration::from_secs(5)).is_err());
    }
}
