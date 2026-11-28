use crate::errors::ToolError;
use std::io::{Read, Write};
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
        self.run_input(args, timeout, None)
    }

    pub(crate) fn run_input(
        &self,
        args: &[String],
        timeout: Duration,
        input: Option<&str>,
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
        cmd.arg("--state").arg(self.0).args(args);
        run_command_input(cmd, timeout, input)
    }
}

/// Run `cmd` in its own process group with piped, bounded output: `(exit code, stdout, last
/// stderr line)`. A command past `timeout` is killed with its group.
pub(crate) fn run_command(
    cmd: Command,
    timeout: Duration,
) -> Result<(i32, Vec<u8>, String), ToolError> {
    run_command_input(cmd, timeout, None)
}

fn run_command_input(
    mut cmd: Command,
    timeout: Duration,
    input: Option<&str>,
) -> Result<(i32, Vec<u8>, String), ToolError> {
    {
        cmd.stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
        }
        let mut child = pg_worker_core::stall::spawn_retrying(&mut cmd)
            .map_err(|e| ToolError::Internal(format!("route adapter could not start: {e}")))?;
        if let Some(input) = input {
            let bytes = input.as_bytes().to_vec();
            let mut stdin = child.stdin.take().unwrap();
            std::thread::spawn(move || {
                let _ = stdin.write_all(&bytes);
            });
        }
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
                // The CLI prints its refusal last; host banners and warnings come first.
                .rev()
                .find(|line| !line.trim().is_empty())
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
    // PG_SPAWND_ADAPTER is process-global: the tests that set it run one at a time.
    static ADAPTER_ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());
    #[test]
    fn timeout_kills_group_and_bounds_capture() {
        let _env = ADAPTER_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let script = dir.join("adapter");
        let pidfile = dir.join("pid");
        pg_worker_core::executable_test_support::write_executable(
            &script,
            format!(
                "#!/bin/sh\necho $$ > '{}'\nsleep 30 &\nwait\n",
                pidfile.display()
            ),
        );
        std::env::set_var("PG_SPAWND_ADAPTER", &script);
        // A loaded host can kill the script before its first line wrote the pid; such a
        // round proves nothing about the group, so it repeats with a longer timeout.
        let mut checked = false;
        for timeout in [300, 1_000, 3_000, 10_000].map(Duration::from_millis) {
            let _ = std::fs::remove_file(&pidfile);
            let start = Instant::now();
            let error = Adapter(&dir).run(&[], timeout).unwrap_err();
            let elapsed = start.elapsed();
            assert!(matches!(error, ToolError::Internal(ref s) if s == "route adapter timed out"));
            // Well under the 30 s sleep: the group was killed, not waited for.
            assert!(elapsed < timeout + Duration::from_secs(10), "{elapsed:?}");
            let Some(pid) = std::fs::read_to_string(&pidfile)
                .ok()
                .and_then(|text| text.trim().parse::<i32>().ok())
            else {
                continue;
            };
            assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
            checked = true;
            break;
        }
        std::env::remove_var("PG_SPAWND_ADAPTER");
        assert!(checked, "the adapter script never started");
        assert_eq!(drain(&b"abcdef"[..], 3), b"abc");
        std::fs::remove_dir_all(dir).unwrap();
    }
    fn escaped_descendant_is_bounded(timeout: Duration) {
        let _env = ADAPTER_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let script = dir.join("adapter");
        {
            pg_worker_core::executable_test_support::write_executable(
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
            );
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
    #[test]
    fn escaped_descendant_after_exit_is_bounded() {
        escaped_descendant_is_bounded(Duration::from_secs(30));
    }
    #[test]
    fn escaped_descendant_after_timeout_is_bounded() {
        escaped_descendant_is_bounded(Duration::from_millis(300));
    }
    #[test]
    fn refusal_is_the_last_stderr_line() {
        let _env = ADAPTER_ENV.lock().unwrap_or_else(|e| e.into_inner());
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let script = dir.join("adapter");
        pg_worker_core::executable_test_support::write_executable(
            &script,
            "#!/bin/sh\necho \"host banner\" >&2\necho \"deprecated config\" >&2\necho \"refused: accepted names: codex\" >&2\necho >&2\nexit 2\n",
        );
        std::env::set_var("PG_SPAWND_ADAPTER", &script);
        let result = Adapter(&dir).run(&[], Duration::from_secs(5));
        std::env::remove_var("PG_SPAWND_ADAPTER");
        let Ok((code, _, line)) = result else {
            panic!("adapter failed")
        };
        assert_eq!((code, line.as_str()), (2, "refused: accepted names: codex"));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
