//! Git entry point. Trailer policy lives exclusively in ci_commit_gate.py.
use std::io::Write;
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
        .output()?;
    if merge.status.success() {
        return Ok(0);
    }
    if merge.status.code() != Some(1) {
        return Err("cannot inspect merge state".into());
    }
    // hooksPath is shared by linked worktrees. Keep this binary paired with
    // its installed validator even when the committing worktree predates it.
    let executable = std::env::current_exe()?;
    let gate = executable
        .parent()
        .and_then(|p| p.parent())
        .ok_or("cannot locate installed validator")?
        .join("ci_commit_gate.py");
    // Git removes editor comments after this hook runs. Validate that same
    // message, honoring core.commentChar, without rewriting the user's file.
    let raw = std::fs::read(message)?;
    let comment = Command::new("git")
        .args(["config", "--get", "core.commentChar"])
        .output()?;
    if !comment.status.success() && comment.status.code() != Some(1) {
        return Err("cannot inspect Git comment prefix".into());
    }
    let prefix = if comment.status.success() {
        String::from_utf8(comment.stdout)?.trim_end().to_owned()
    } else {
        "#".to_owned()
    };
    let scissors = format!("{prefix} ------------------------ >8 ------------------------");
    let cut = raw
        .split_inclusive(|b| *b == b'\n')
        .scan(0, |offset, line| {
            let start = *offset;
            *offset += line.len();
            Some((start, line))
        })
        .find(|(_, line)| line.strip_suffix(b"\n").unwrap_or(line) == scissors.as_bytes())
        .map_or(raw.len(), |(offset, _)| offset);
    let mut cleaner = Command::new("git")
        .args(["stripspace", "--strip-comments"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let input = cleaner.stdin.take().ok_or("missing cleaner stdin")?;
    let cleaned = std::thread::scope(|scope| -> Result<_, std::io::Error> {
        let writer = scope.spawn(move || {
            let mut input = input;
            input.write_all(&raw[..cut])
        });
        let output = cleaner.wait_with_output();
        writer
            .join()
            .map_err(|_| std::io::Error::other("message writer panicked"))??;
        output
    })?;
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
