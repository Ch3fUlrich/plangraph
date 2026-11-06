//! Distinguish negative ancestry from infrastructure failure without leaking diagnostics.
use pg_worker_core::git::{GitError, git};
use std::path::Path;

pub(crate) fn ensure(repo: &Path, base: &str, head: &str) -> Result<(), String> {
    classify(git(repo, &["merge-base", "--is-ancestor", base, head]))
}

fn classify(result: Result<String, GitError>) -> Result<(), String> {
    match result {
        Ok(_) => Ok(()),
        Err(GitError::Failed { code: Some(1), .. }) => {
            Err("phase branch does not contain the base head".to_string())
        }
        // Never forward stderr, argv or paths into activity history.
        Err(_) => Err("phase branch ancestry check failed".to_string()),
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
                "phase branch does not contain the base head"
            } else {
                "phase branch ancestry check failed"
            };
            assert_eq!(classify(Err(error)).unwrap_err(), expected);
        }
        assert!(classify(Ok(String::new())).is_ok());
        assert_eq!(
            classify(Err(GitError::Io(std::io::Error::other("private-io")))).unwrap_err(),
            "phase branch ancestry check failed"
        );
    }
}
