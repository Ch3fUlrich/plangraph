use crate::errors::ToolError;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub(crate) struct Adapter<'a>(pub &'a Path);
fn drain(mut reader: impl Read, cap: usize) -> Vec<u8> {
    let mut kept = Vec::new();
    let mut buf = [0; 8192];
    while let Ok(n) = reader.read(&mut buf) {
        if n == 0 {
            break;
        }
        kept.extend_from_slice(&buf[..n.min(cap - kept.len())]);
    }
    kept
}
impl Adapter<'_> {
    pub(crate) fn run(
        &self,
        args: &[String],
        timeout: Duration,
    ) -> Result<(i32, Vec<u8>, String), ToolError> {
        let mut cmd = if let Some(program) = std::env::var_os("PG_SPAWND_ADAPTER") {
            if !Path::new(&program).is_absolute() {
                return Err(ToolError::Internal("route adapter could not start".into()));
            }
            Command::new(program)
        } else {
            let mut cmd = Command::new(
                std::env::var_os("PG_SPAWND_PYTHON")
                    .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into()),
            );
            cmd.args(["-m", "plangraph.spawner.cli"]);
            cmd
        };
        cmd.arg("--state")
            .arg(self.0)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
        }
        let mut child = cmd
            .spawn()
            .map_err(|_| ToolError::Internal("route adapter could not start".into()))?;
        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let (send, receive) = std::sync::mpsc::channel();
        let out_send = send.clone();
        std::thread::spawn(move || {
            let _ = out_send.send((true, drain(stdout, 4 * 1024 * 1024)));
        });
        std::thread::spawn(move || {
            let _ = send.send((false, drain(stderr, 8192)));
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
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        for _ in 0..2 {
            // Detach blocked readers: escaped descendants can keep their pipes open.
            let (is_stdout, bytes) = receive
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .map_err(|_| ToolError::Internal("route adapter timed out".into()))?;
            if is_stdout {
                stdout = bytes;
            } else {
                stderr = bytes;
            }
        }
        let status = status.ok_or_else(|| ToolError::Internal("route adapter timed out".into()))?;
        let line = pg_worker_core::redact::redact(
            String::from_utf8_lossy(&stderr)
                .lines()
                .next()
                .unwrap_or(""),
        )
        .chars()
        .take(300)
        .collect();
        Ok((status.code().unwrap_or(-1), stdout, line))
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn timeout_kills_group_and_bounds_capture() {
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let script = dir.join("adapter");
        let pidfile = dir.join("pid");
        std::fs::write(
            &script,
            format!(
                "#!/bin/sh\necho $$ > '{}'\nsleep 30 &\nwait\n",
                pidfile.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::env::set_var("PG_SPAWND_ADAPTER", &script);
        let start = Instant::now();
        let error = Adapter(&dir)
            .run(&[], Duration::from_millis(300))
            .unwrap_err();
        std::env::remove_var("PG_SPAWND_ADAPTER");
        assert!(matches!(error, ToolError::Internal(ref s) if s == "route adapter timed out"));
        assert!(start.elapsed() < Duration::from_secs(2));
        let pid: i32 = std::fs::read_to_string(pidfile)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
        assert_eq!(drain(&b"abcdef"[..], 3), b"abc");
        for timeout in [Duration::from_secs(30), Duration::from_millis(300)] {
            std::fs::write(
                &script,
                format!(
                    "#!/bin/sh\nsetsid sleep 30 &\necho $! > '{}'\nprintf '{{}}'\n{}\n",
                    dir.join("escaped-pid").display(),
                    if timeout.as_secs() == 30 {
                        "exit 0"
                    } else {
                        "sleep 30"
                    }
                ),
            )
            .unwrap();
            std::env::set_var("PG_SPAWND_ADAPTER", &script);
            let start = Instant::now();
            let result = Adapter(&dir).run(&[], timeout);
            std::env::remove_var("PG_SPAWND_ADAPTER");
            let elapsed = start.elapsed();
            let pid: i32 = std::fs::read_to_string(dir.join("escaped-pid"))
                .unwrap()
                .trim()
                .parse()
                .unwrap();
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
            assert!(
                matches!(result, Err(ToolError::Internal(ref s)) if s == "route adapter timed out")
            );
            assert!(elapsed < Duration::from_secs(4));
        }
        std::fs::remove_dir_all(dir).unwrap();
    }
}
