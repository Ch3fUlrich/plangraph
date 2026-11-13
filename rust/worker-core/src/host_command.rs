//! Bounded host commands with isolated Git environment and file-backed output.
use std::{
    fs::File,
    io::{self, Read, Seek},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub fn isolate_git(command: &mut Command) {
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    command.env("GIT_TERMINAL_PROMPT", "0").stdin(Stdio::null());
}
pub fn output(command: &mut Command, timeout: Duration) -> io::Result<Vec<u8>> {
    let mut file: File = tempfile::tempfile()?;
    isolate_git(command);
    command.stdout(file.try_clone()?).stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn()?;
    let start = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break Some(status);
        }
        if start.elapsed() >= timeout {
            break None;
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    #[cfg(unix)]
    // SAFETY: this process owns the child's isolated process group.
    unsafe {
        libc::killpg(child.id() as i32, libc::SIGKILL);
    }
    if status.is_none() {
        let _ = child.kill();
    }
    let _ = child.wait();
    if !status.is_some_and(|s| s.success()) {
        return Err(io::Error::other("command failed or timed out"));
    }
    file.rewind()?;
    let mut bytes = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err(io::Error::other("command output too large"));
    }
    Ok(bytes)
}
