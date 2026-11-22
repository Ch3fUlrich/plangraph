//! Registry object availability; no writer or publication policy.
use crate::{hosts::RepoEntry, lockfile};
use std::io::{Read, Seek, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

#[derive(Debug, thiserror::Error)]
pub enum ResolveError {
    #[error("invalid_workspace")]
    Invalid(&'static str),
    #[error("io")]
    Io(#[from] std::io::Error),
    #[error("git_failed")]
    Git,
    #[error("fetch_lock")]
    Lock,
    #[error("sha_not_reachable")]
    ShaNotReachable,
    #[error("bundle_too_large")]
    BundleTooLarge,
    #[error("workspace_verify")]
    Verify(&'static str),
}

pub fn git_command(root: &Path) -> Command {
    let mut cmd = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            cmd.env_remove(key);
        }
    }
    cmd.current_dir(root)
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdin(Stdio::null());
    #[cfg(test)]
    cmd.env("HOME", root)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", root.join("absent-config"));
    cmd
}

/// Capture Git stdout without a pipe reader that could outlive a timed-out child.
pub fn git_output_until(
    command: &mut Command,
    deadline: Instant,
    cap: usize,
) -> Result<(ExitStatus, Vec<u8>), ResolveError> {
    let mut output = tempfile::tempfile()?;
    command.stdout(output.try_clone()?).stderr(Stdio::null());
    let status = crate::stall::status_until(command, deadline)?;
    output.rewind()?;
    let mut bytes = Vec::new();
    output
        .take(cap.saturating_add(1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > cap {
        return Err(ResolveError::Git);
    }
    Ok((status, bytes))
}

struct Git<'a> {
    root: &'a Path,
    deadline: Option<Instant>,
}

impl Git<'_> {
    fn run(
        &self,
        args: &[&str],
        input: Option<&[u8]>,
    ) -> Result<(ExitStatus, Vec<u8>), ResolveError> {
        let mut command = git_command(self.root);
        command.args(args).stderr(Stdio::null());
        if let Some(bytes) = input {
            let mut file = tempfile::tempfile()?;
            file.write_all(bytes)?;
            file.rewind()?;
            command.stdin(file);
        }
        if let Some(deadline) = self.deadline {
            git_output_until(&mut command, deadline, 1024 * 1024)
        } else {
            let out = command.output()?;
            Ok((out.status, out.stdout))
        }
    }

    fn output(&self, args: &[&str]) -> Result<Vec<u8>, ResolveError> {
        let (status, stdout) = self.run(args, None)?;
        if !status.success() {
            return Err(ResolveError::Git);
        }
        Ok(stdout)
    }

    fn common_dir(&self) -> Result<PathBuf, ResolveError> {
        let bytes = self.output(&["rev-parse", "--git-common-dir"])?;
        let text = std::str::from_utf8(&bytes).map_err(|_| ResolveError::Git)?;
        Ok(self
            .root
            .join(text.trim_end_matches(['\r', '\n']))
            .canonicalize()?)
    }

    fn has_commit(&self, sha: &str) -> Result<bool, ResolveError> {
        let revision = format!("{sha}^{{commit}}");
        let (status, _) = self.run(&["cat-file", "-e", &revision], None)?;
        if status.success() {
            return Ok(true);
        }
        // A failed peel is a reachability miss for missing or non-commit objects.
        let query = format!("{sha}\n");
        let (status, stdout) = self.run(&["cat-file", "--batch-check"], Some(query.as_bytes()))?;
        let response = String::from_utf8_lossy(&stdout);
        let fields: Vec<_> = response.split_whitespace().collect();
        let non_commit = matches!(fields.as_slice(), [oid, "blob" | "tree" | "tag", size]
            if *oid == sha && size.parse::<u64>().is_ok());
        if status.success() && (stdout == format!("{sha} missing\n").as_bytes() || non_commit) {
            Ok(false)
        } else {
            Err(ResolveError::Git)
        }
    }
}

/// Ensure a commit exists locally, fetching only the registry origin on a miss.
pub fn ensure_commit(repo: &RepoEntry, sha: &str, wait: Duration) -> Result<(), ResolveError> {
    ensure_commit_inner(repo, sha, wait, None)
}

/// Ensure a commit exists within a shared deadline for lock waits and Git subprocesses.
pub fn ensure_commit_until(
    repo: &RepoEntry,
    sha: &str,
    wait: Duration,
    deadline: Instant,
) -> Result<(), ResolveError> {
    ensure_commit_inner(repo, sha, wait, Some(deadline))
}

fn ensure_commit_inner(
    repo: &RepoEntry,
    sha: &str,
    wait: Duration,
    deadline: Option<Instant>,
) -> Result<(), ResolveError> {
    if sha.len() != 40
        || !sha
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(ResolveError::Invalid("sha"));
    }
    let git = Git {
        root: Path::new(&repo.root),
        deadline,
    };
    if git.has_commit(sha)? {
        return Ok(());
    }
    let common = git.common_dir()?;
    let wait = deadline.map_or(wait, |end| {
        wait.min(end.saturating_duration_since(Instant::now()))
    });
    let _guard =
        lockfile::lock_with_retry(&common.join("spawner-fetch.lock"), wait, &mut rand::rng())
            .map_err(|_| ResolveError::Lock)?;
    if git.common_dir()? != common {
        return Err(ResolveError::Verify("common_dir"));
    }
    if git.has_commit(sha)? {
        return Ok(());
    }
    let remote = git.output(&["remote", "get-url", "--all", "origin"])?;
    let remote = std::str::from_utf8(&remote).map_err(|_| ResolveError::Git)?;
    if remote.trim_end_matches(['\r', '\n']) != repo.remote {
        return Err(ResolveError::Verify("remote"));
    }
    git.output(&["fetch", "--", "origin"])?;
    if git.has_commit(sha)? {
        Ok(())
    } else {
        Err(ResolveError::ShaNotReachable)
    }
}

#[cfg(test)]
#[path = "workspace_resolver_tests.rs"]
mod tests;
