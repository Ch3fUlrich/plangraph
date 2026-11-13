//! Bounded, shell-free repository probes. Output files avoid pipe deadlocks.
use std::{
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub fn run(repo: &Path, program: &str, args: &[&str]) -> Result<String, String> {
    let output = tempfile::tempfile().map_err(|_| "probe output unavailable")?;
    let mut command = Command::new(program);
    command
        .current_dir(repo)
        .args(args)
        .stdin(Stdio::null())
        .stdout(output.try_clone().map_err(|_| "probe output unavailable")?)
        .stderr(Stdio::null());
    for (key, _) in std::env::vars_os() {
        let name = key.to_string_lossy();
        if matches!(
            name.as_ref(),
            "GIT_DIR" | "GIT_WORK_TREE" | "GIT_INDEX_FILE"
        ) || name.starts_with("GIT_CONFIG")
        {
            command.env_remove(key);
        }
    }
    command
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GCM_INTERACTIVE", "never");
    let mut child = command.spawn().map_err(|_| "probe could not start")?;
    let start = Instant::now();
    loop {
        match child.try_wait().map_err(|_| "probe wait failed")? {
            Some(status) if status.success() => break,
            Some(_) => return Err("repository probe failed".into()),
            None if start.elapsed() > Duration::from_secs(30) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("repository probe timed out".into());
            }
            None => std::thread::sleep(Duration::from_millis(10)),
        }
    }
    use std::io::{Read, Seek};
    let mut output = output;
    if output
        .metadata()
        .map_err(|_| "probe output unavailable")?
        .len()
        > 128 * 1024 * 1024
    {
        return Err("repository probe output too large".into());
    }
    output.rewind().map_err(|_| "probe output unavailable")?;
    let mut text = String::new();
    output
        .take(128 * 1024 * 1024)
        .read_to_string(&mut text)
        .map_err(|_| "probe output invalid")?;
    Ok(text)
}
