//! Outcome probe: run before retrying an attempt to see what it left behind.

use crate::check::run_check;
use crate::git::{git, GitError};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::time::Duration;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProbeOutcome {
    /// Commits beyond base and the check passes: nothing left to do.
    AlreadyDone,
    /// Commits beyond base but the check fails: resume from them.
    Partial,
    /// No commits beyond base.
    Nothing,
}

#[derive(Debug, thiserror::Error)]
pub enum ProbeError {
    #[error(transparent)]
    Git(#[from] GitError),
    #[error("check could not run: {0}")]
    Check(#[from] std::io::Error),
}

/// Number of commits on HEAD of `dir` beyond `base`.
pub fn commits_beyond(dir: &Path, base: &str) -> Result<u32, GitError> {
    let n = git(dir, &["rev-list", "--count", &format!("{base}..HEAD")])?;
    Ok(n.parse().unwrap_or(0))
}

/// Probe an attempt worktree. `dir` should be the attempt's worktree with its
/// branch checked out; the check runs against what is committed and present there.
pub fn probe(
    dir: &Path,
    base: &str,
    check_cmd: &str,
    timeout: Duration,
    tail_n: usize,
) -> Result<ProbeOutcome, ProbeError> {
    if commits_beyond(dir, base)? == 0 {
        return Ok(ProbeOutcome::Nothing);
    }
    let r = run_check(check_cmd, dir, timeout, tail_n)?;
    Ok(if r.passed() {
        ProbeOutcome::AlreadyDone
    } else {
        ProbeOutcome::Partial
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::{commit_all, repo, write};

    const T: Duration = Duration::from_secs(20);

    #[test]
    fn nothing_without_commits_even_if_dirty() {
        let (d, base) = repo();
        write(d.path(), "wip.txt", "x");
        assert_eq!(
            probe(d.path(), &base, "true", T, 10).unwrap(),
            ProbeOutcome::Nothing
        );
    }

    #[test]
    fn already_done_when_check_passes() {
        let (d, base) = repo();
        write(d.path(), "done.txt", "x");
        commit_all(d.path(), "work");
        assert_eq!(commits_beyond(d.path(), &base).unwrap(), 1);
        let out = probe(d.path(), &base, "test -f done.txt", T, 10).unwrap();
        assert_eq!(out, ProbeOutcome::AlreadyDone);
    }

    #[test]
    fn partial_when_check_fails() {
        let (d, base) = repo();
        write(d.path(), "half.txt", "x");
        commit_all(d.path(), "work");
        let out = probe(d.path(), &base, "test -f other.txt", T, 10).unwrap();
        assert_eq!(out, ProbeOutcome::Partial);
    }

    #[test]
    fn bad_base_is_an_error() {
        let (d, _) = repo();
        assert!(probe(d.path(), "deadbeef", "true", T, 10).is_err());
    }
}
