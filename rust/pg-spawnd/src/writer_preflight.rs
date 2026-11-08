use std::{
    io,
    process::{Command, Output, Stdio},
    time::{Duration, Instant},
};

pub(crate) fn refuse_collection(
    cwd: &std::path::Path,
    prompt: &serde_json::Value,
    base: &mut serde_json::Value,
) -> bool {
    let error = collect(cwd, prompt.as_str().unwrap_or("")).unwrap_or_else(|e| Some(e.to_string()));
    if let Some(error) = error {
        base["reason"] = serde_json::json!("test_env_unready");
        base["error"] = serde_json::json!(error);
        return true;
    }
    false
}

fn collect(cwd: &std::path::Path, prompt: &str) -> io::Result<Option<String>> {
    let tests: Vec<_> = prompt
        .split(|c: char| c.is_whitespace() || "`\"'(),;".contains(c))
        .filter(|s| s.starts_with("tests/") && (s.ends_with(".py") || s.contains(".py::")))
        .collect();
    if tests.is_empty() {
        return Ok(None);
    }
    let output = Command::new("python3")
        .args(["-m", "pytest", "--collect-only", "-q"])
        .args(tests)
        .current_dir(cwd)
        .output()?;
    Ok((!output.status.success()).then(|| {
        format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    }))
}

pub(super) fn run(cwd: &str) -> io::Result<Output> {
    let mut child = Command::new("python3")
        .args(["tools/preflight.py", "--writer-completion"])
        .current_dir(cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let start = Instant::now();
    loop {
        if child.try_wait()?.is_some() {
            return child.wait_with_output();
        }
        if start.elapsed() >= Duration::from_secs(1800) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(io::ErrorKind::TimedOut, "preflight timeout"));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
