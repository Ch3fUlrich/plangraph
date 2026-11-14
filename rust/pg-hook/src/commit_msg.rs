//! Git entry point. Trailer policy lives exclusively in ci_commit_gate.py.
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

fn run() -> Result<i32, Box<dyn std::error::Error>> {
    let message = std::env::args_os().nth(1).ok_or("missing message file")?;
    let root = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .output()?;
    if !root.status.success() {
        return Err("cannot locate checkout".into());
    }
    // MERGE_HEAD is worktree-specific. Git also invokes commit-msg for merges.
    let merge = Command::new("git")
        .args(["rev-parse", "--verify", "-q", "MERGE_HEAD"])
        .status()?;
    if merge.success() {
        return Ok(0);
    }
    if merge.code() != Some(1) {
        return Err("cannot inspect merge state".into());
    }
    let gate =
        PathBuf::from(String::from_utf8(root.stdout)?.trim_end()).join("tools/ci_commit_gate.py");
    // Git removes editor comments after this hook runs. Validate that same
    // message, honoring core.commentChar, without rewriting the user's file.
    let cleaned = Command::new("git")
        .args(["stripspace", "--strip-comments"])
        .stdin(std::fs::File::open(message)?)
        .output()?;
    if !cleaned.status.success() {
        return Err("cannot clean commit message".into());
    }
    let mut gate = Command::new("python3")
        .arg(gate)
        .arg("commit-message")
        .arg("-")
        .stdin(Stdio::piped())
        .spawn()?;
    gate.stdin
        .take()
        .ok_or("missing gate stdin")?
        .write_all(&cleaned.stdout)?;
    let status = gate.wait()?;
    Ok(status.code().unwrap_or(1))
}

fn main() -> ExitCode {
    match run() {
        Ok(0) => ExitCode::SUCCESS,
        Ok(_) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("commit-msg: {error}");
            ExitCode::FAILURE
        }
    }
}
