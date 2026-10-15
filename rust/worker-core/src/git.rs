//! Thin wrapper over the `git` CLI.

use std::path::Path;
use std::process::Command;

#[derive(Debug, thiserror::Error)]
pub enum GitError {
    #[error("io error running git: {0}")]
    Io(#[from] std::io::Error),
    #[error("git {args} failed (exit {code:?}): {stderr}")]
    Failed {
        args: String,
        code: Option<i32>,
        stderr: String,
    },
    #[error(
        "refusing revision {0:?}: empty, option-like or containing whitespace/control characters"
    )]
    BadRev(String),
}

/// Reject a caller-supplied revision that git could parse as an option
/// (leading `-`) or that is not a single token, before it reaches an argv.
pub fn check_rev(rev: &str) -> Result<(), GitError> {
    if rev.is_empty()
        || rev.starts_with('-')
        || rev.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(GitError::BadRev(rev.to_string()));
    }
    Ok(())
}

/// Test builds only: no system or global config, and a fixed identity, so tests
/// behave the same on a machine (or CI runner) without a git identity.
fn hermetic(cmd: &mut Command) {
    cmd.env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "tester")
        .env("GIT_AUTHOR_EMAIL", "t@example.com")
        .env("GIT_COMMITTER_NAME", "tester")
        .env("GIT_COMMITTER_EMAIL", "t@example.com");
}

/// Run `git <args>` in `dir` and return raw stdout bytes.
pub fn git_bytes(dir: &Path, args: &[&str]) -> Result<Vec<u8>, GitError> {
    let mut cmd = Command::new("git");
    if cfg!(test) {
        hermetic(&mut cmd);
    }
    let out = cmd
        .args(args)
        .current_dir(dir)
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdin(std::process::Stdio::null())
        .output()?;
    if out.status.success() {
        Ok(out.stdout)
    } else {
        Err(GitError::Failed {
            args: args.join(" "),
            code: out.status.code(),
            stderr: String::from_utf8_lossy(&out.stderr).trim().to_string(),
        })
    }
}

/// Run `git <args>` in `dir` and return trimmed stdout as a string.
pub fn git(dir: &Path, args: &[&str]) -> Result<String, GitError> {
    let b = git_bytes(dir, args)?;
    Ok(String::from_utf8_lossy(&b).trim().to_string())
}
