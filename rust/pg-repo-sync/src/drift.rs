//! Local checkout config inspection with no writes, independent of inherited Git context.
use std::{
    path::Path,
    process::{Command, Stdio},
};

#[derive(Default)]
pub struct Policy {
    pub host: String,
    pub names: Vec<String>,
    pub emails: Vec<String>,
}
pub fn git(repo: &Path, args: &[&str]) -> Command {
    let mut cmd = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(key);
        }
    }
    cmd.arg("-C")
        .arg(repo)
        .args(["-c", "core.hooksPath=/dev/null"])
        .args(args)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    cmd
}
pub fn output(repo: &Path, args: &[&str]) -> Result<std::process::Output, Option<i32>> {
    bounded(git(repo, args))
}
pub fn bounded(mut cmd: Command) -> Result<std::process::Output, Option<i32>> {
    use std::{sync::mpsc, time::Duration};
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let child = cmd.stdout(Stdio::piped()).spawn().map_err(|_| None)?;
    let pid = child.id();
    let (send, recv) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = send.send(child.wait_with_output());
    });
    let seconds = std::env::var("PG_REPO_SYNC_TIMEOUT_SECS")
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(60)
        .clamp(1, 60);
    match recv.recv_timeout(Duration::from_secs(seconds)) {
        Ok(Ok(out)) => Ok(out),
        _ => {
            #[cfg(unix)]
            unsafe {
                unsafe extern "C" {
                    fn kill(pid: i32, signal: i32) -> i32;
                }
                // SAFETY: the owned child has a dedicated process group.
                kill(-(pid as i32), 9);
            }
            let _ = recv.recv_timeout(Duration::from_secs(1));
            Err(None)
        }
    }
}
pub fn text(repo: &Path, args: &[&str]) -> Result<String, Option<i32>> {
    let out = output(repo, args)?;
    if !out.status.success() {
        return Err(out.status.code());
    }
    String::from_utf8(out.stdout)
        .map(|s| s.trim().into())
        .map_err(|_| None)
}
pub fn is_checkout(repo: &Path) -> bool {
    if !repo.join(".git").exists() {
        return false;
    }
    let top = text(repo, &["rev-parse", "--show-toplevel"])
        .ok()
        .and_then(|s| std::fs::canonicalize(s).ok());
    if top.is_some() {
        return top == std::fs::canonicalize(repo).ok();
    }
    // A corrupt core.bare override still needs a local config drift report.
    text(repo, &["rev-parse", "--absolute-git-dir"])
        .ok()
        .and_then(|s| std::fs::canonicalize(s).ok())
        == std::fs::canonicalize(repo.join(".git")).ok()
}
pub fn check(repo: &Path, policy: &Policy) -> Vec<String> {
    let mut differences = Vec::new();
    match text(repo, &["config", "--local", "--bool", "--get", "core.bare"]) {
        Ok(value) if value == "false" => {}
        Err(Some(1)) => {} // Git's absent core.bare default is false.
        Ok(value) => differences.push(format!("core.bare={value:?}")),
        Err(_) => differences.push("core.bare=unreadable".into()),
    }
    for (key, allowed) in [("user.name", &policy.names), ("user.email", &policy.emails)] {
        let values =
            output(repo, &["config", "--local", "--null", "--get-all", key]).and_then(|out| {
                if !out.status.success() {
                    return Err(out.status.code());
                }
                String::from_utf8(out.stdout).map_err(|_| None)
            });
        match values {
            Ok(values) => {
                for value in values.split_terminator('\0') {
                    if !allowed.iter().any(|a| a == value) {
                        differences.push(format!("{key}={value:?}"));
                    }
                }
            }
            Err(Some(1)) => {} // No local override: host's normal inherited identity applies.
            Err(_) => differences.push(format!("{key}=unreadable")),
        }
    }
    differences
}

pub fn live_paths() -> Result<Vec<std::path::PathBuf>, ()> {
    let mut paths = Vec::new();
    for scope in [None, Some("--user")] {
        let query = |args: &[&str]| -> Result<String, ()> {
            let mut cmd = Command::new("systemctl");
            cmd.args(scope)
                .args(args)
                .stdin(Stdio::null())
                .stderr(Stdio::null());
            let out = bounded(cmd).map_err(|_| ())?;
            if !out.status.success() {
                return Err(());
            }
            String::from_utf8(out.stdout).map_err(|_| ())
        };
        let units = query(&[
            "list-units",
            "--state=running",
            "--type=service",
            "--plain",
            "--no-legend",
            "--no-pager",
        ])?;
        for unit in units.lines().filter_map(|s| s.split_whitespace().next()) {
            let dir = query(&["show", "--property=WorkingDirectory", "--value", unit])?;
            if !dir.trim().is_empty() {
                paths.push(std::fs::canonicalize(dir.trim()).map_err(|_| ())?);
            }
        }
    }
    Ok(paths)
}
