//! Registry object availability; no writer or publication policy.
use crate::{hosts::RepoEntry, lockfile};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

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
    #[cfg(any(test, feature = "test-support"))]
    let mut cmd = crate::git_test_support::command(root);
    #[cfg(not(any(test, feature = "test-support")))]
    let mut cmd = Command::new("git");
    #[cfg(not(any(test, feature = "test-support")))]
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
    cmd
}

fn output(root: &Path, args: &[&str]) -> Result<Vec<u8>, ResolveError> {
    let out = git_command(root).args(args).output()?;
    if !out.status.success() {
        return Err(ResolveError::Git);
    }
    Ok(out.stdout)
}

fn common_dir(root: &Path) -> Result<PathBuf, ResolveError> {
    let bytes = output(root, &["rev-parse", "--git-common-dir"])?;
    let text = std::str::from_utf8(&bytes).map_err(|_| ResolveError::Git)?;
    Ok(root
        .join(text.trim_end_matches(['\r', '\n']))
        .canonicalize()?)
}

fn has_commit(root: &Path, sha: &str) -> Result<bool, ResolveError> {
    let revision = format!("{sha}^{{commit}}");
    let out = git_command(root)
        .args(["cat-file", "-e", &revision])
        .output()?;
    if out.status.success() {
        return Ok(true);
    }
    // A failed peel is a reachability miss for missing or non-commit objects.
    let mut child = git_command(root)
        .args(["cat-file", "--batch-check"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    use std::io::Write;
    let write = writeln!(child.stdin.take().ok_or(ResolveError::Git)?, "{sha}");
    let checked = child.wait_with_output()?;
    write?;
    let response = String::from_utf8_lossy(&checked.stdout);
    let fields: Vec<_> = response.split_whitespace().collect();
    let non_commit = matches!(fields.as_slice(), [oid, "blob" | "tree" | "tag", size]
        if *oid == sha && size.parse::<u64>().is_ok());
    if checked.status.success()
        && (checked.stdout == format!("{sha} missing\n").as_bytes() || non_commit)
    {
        Ok(false)
    } else {
        Err(ResolveError::Git)
    }
}

/// Ensure a commit exists locally, fetching only the registry origin on a miss.
pub fn ensure_commit(repo: &RepoEntry, sha: &str, wait: Duration) -> Result<(), ResolveError> {
    if sha.len() != 40
        || !sha
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(ResolveError::Invalid("sha"));
    }
    let root = Path::new(&repo.root);
    if has_commit(root, sha)? {
        return Ok(());
    }
    let common = common_dir(root)?;
    let _guard =
        lockfile::lock_with_retry(&common.join("spawner-fetch.lock"), wait, &mut rand::rng())
            .map_err(|_| ResolveError::Lock)?;
    if common_dir(root)? != common {
        return Err(ResolveError::Verify("common_dir"));
    }
    if has_commit(root, sha)? {
        return Ok(());
    }
    let remote = output(root, &["remote", "get-url", "--all", "origin"])?;
    let remote = std::str::from_utf8(&remote).map_err(|_| ResolveError::Git)?;
    if remote.trim_end_matches(['\r', '\n']) != repo.remote {
        return Err(ResolveError::Verify("remote"));
    }
    output(root, &["fetch", "--", "origin"])?;
    if has_commit(root, sha)? {
        Ok(())
    } else {
        Err(ResolveError::ShaNotReachable)
    }
}

#[cfg(test)]
#[path = "workspace_resolver_tests.rs"]
mod tests;
