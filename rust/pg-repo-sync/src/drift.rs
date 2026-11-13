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
pub fn text(repo: &Path, args: &[&str]) -> Result<String, Option<i32>> {
    let output = git(repo, args)
        .stdout(Stdio::piped())
        .output()
        .map_err(|_| None)?;
    if !output.status.success() {
        return Err(output.status.code());
    }
    String::from_utf8(output.stdout)
        .map(|s| s.trim().to_owned())
        .map_err(|_| None)
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
        match text(repo, &["config", "--local", "--get-all", key]) {
            Ok(values) => {
                for value in values.lines() {
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
