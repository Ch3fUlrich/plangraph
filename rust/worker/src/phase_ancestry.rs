//! Distinguish negative ancestry from infrastructure failure without leaking diagnostics.
use pg_worker_core::git::{GitError, git};
use std::path::Path;

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum AncestryError {
    Diverged,
    CheckFailed,
}

impl std::fmt::Display for AncestryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Diverged => "phase branch does not contain the base head",
            Self::CheckFailed => "phase branch ancestry check failed",
        })
    }
}

pub(crate) fn ensure(repo: &Path, base: &str, head: &str) -> Result<(), AncestryError> {
    classify(git(repo, &["merge-base", "--is-ancestor", base, head]))
}

fn classify(result: Result<String, GitError>) -> Result<(), AncestryError> {
    match result {
        Ok(_) => Ok(()),
        Err(GitError::Failed { code: Some(1), .. }) => Err(AncestryError::Diverged),
        // Never forward stderr, argv or paths into activity history.
        Err(_) => Err(AncestryError::CheckFailed),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn distinguishes_negative_ancestry_from_infrastructure_errors() {
        for code in [Some(1), Some(2), Some(128), None] {
            let error = GitError::Failed {
                args: "private-argument".into(),
                code,
                stderr: "private-diagnostic".into(),
            };
            let expected = if code == Some(1) {
                AncestryError::Diverged
            } else {
                AncestryError::CheckFailed
            };
            assert_eq!(classify(Err(error)).unwrap_err(), expected);
        }
        assert!(classify(Ok(String::new())).is_ok());
        assert_eq!(
            classify(Err(GitError::Io(std::io::Error::other("private-io")))).unwrap_err(),
            AncestryError::CheckFailed
        );
    }
}
