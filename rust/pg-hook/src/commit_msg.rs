//! Git entry point. Trailer policy lives exclusively in ci_commit_gate.py.
use std::io::Write;
use std::path::Path;
use std::process::{Command, ExitCode, Stdio};

fn install(root: &Path) -> Result<i32, Box<dyn std::error::Error>> {
    let root = root.canonicalize()?;
    let hooks = root.join("tools/hooks");
    if !root.join("tools/ci_commit_gate.py").is_file() {
        return Err("checkout has no commit gate".into());
    }
    let executable = std::env::current_exe()?;
    let destination = hooks.join("commit-msg");
    if destination.canonicalize().ok().as_ref() != Some(&executable) {
        std::fs::copy(&executable, &destination)?;
    }
    std::fs::set_permissions(&destination, std::fs::metadata(&executable)?.permissions())?;
    for args in [
        vec!["config", "extensions.worktreeConfig", "true"],
        vec![
            "config",
            "--worktree",
            "core.hooksPath",
            hooks.to_str().ok_or("non-UTF-8 hooks path")?,
        ],
    ] {
        if !Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(args)
            .status()?
            .success()
        {
            return Err("cannot configure worktree Git hooks".into());
        }
    }
    println!("Git hooks installed: {}", hooks.display());
    Ok(0)
}

fn run() -> Result<i32, Box<dyn std::error::Error>> {
    let message = std::env::args_os().nth(1).ok_or("missing message file")?;
    if message == "--install" {
        let root = std::env::args_os().nth(2).ok_or("missing checkout path")?;
        return install(Path::new(&root));
    }
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
    // Git chooses the prefix for commentString/auto. The scissors suffix is
    // fixed; do not second-guess its choice by reading only commentChar.
    let scissors = b" ------------------------ >8 ------------------------";
    let marker = raw
        .split_inclusive(|b| *b == b'\n')
        .scan(0, |offset, line| {
            let start = *offset;
            *offset += line.len();
            Some((start, line))
        })
        .find(|(_, line)| line.strip_suffix(b"\n").unwrap_or(line).ends_with(scissors));
    let cut = marker.map_or(raw.len(), |(offset, _)| offset);
    let mut command = Command::new("git");
    if let Some((_, line)) = marker {
        let line = line.strip_suffix(b"\n").unwrap_or(line);
        let prefix = std::str::from_utf8(&line[..line.len() - scissors.len()])?;
        // auto may choose a different prefix because the message contains '#'.
        if !prefix.is_empty() {
            command
                .arg("-c")
                .arg(format!("core.commentString={prefix}"));
            if prefix.len() == 1 {
                command.arg("-c").arg(format!("core.commentChar={prefix}"));
            }
        }
    }
    let mut cleaner = command
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
