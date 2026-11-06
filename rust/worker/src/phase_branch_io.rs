//! Branch creation and checked adoption, with sanitized infrastructure errors.
use super::*;

#[derive(Debug)]
pub(crate) enum BranchError {
    Refusal(String),
    Io,
    Ancestry(String),
}

impl std::fmt::Display for BranchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Refusal(message) | Self::Ancestry(message) => f.write_str(message),
            Self::Io => f.write_str("phase branch IO failed"),
        }
    }
}

impl From<String> for BranchError {
    fn from(message: String) -> Self {
        Self::Refusal(message)
    }
}

pub(super) fn ensure_phase_branch_with(
    input: &PhaseBranchInput,
    mut run: impl FnMut(&Path, &[&str]) -> Result<String, pg_worker_core::git::GitError>,
) -> Result<PhaseBranchResult, BranchError> {
    if input.repo.is_empty() || input.base_branch.is_empty() || input.branch.is_empty() {
        return Err("repo, base_branch and branch are required"
            .to_string()
            .into());
    }
    check_rev(&input.base_branch).map_err(|_| "invalid base revision".to_string())?;
    check_rev(&input.branch).map_err(|_| "invalid phase revision".to_string())?;
    if !input.branch.starts_with("phase/") {
        return Err("not a phase/ branch".to_string().into());
    }
    if input.branch == input.base_branch {
        return Err("phase branch must not be the base branch"
            .to_string()
            .into());
    }
    let repo = Path::new(&input.repo);
    let base_ref = format!("refs/heads/{}", input.base_branch);
    let branch_ref = format!("refs/heads/{}", input.branch);
    let base_head = run(repo, &["rev-parse", base_ref.as_str()]).map_err(branch_git_error)?;
    match run(repo, &["rev-parse", "--verify", "-q", branch_ref.as_str()]) {
        Ok(head) => {
            let base_head =
                run(repo, &["rev-parse", base_ref.as_str()]).map_err(branch_git_error)?;
            crate::phase_ancestry::ensure(repo, &base_head, &head)
                .map_err(BranchError::Ancestry)?;
            return Ok(PhaseBranchResult {
                head,
                created: false,
            });
        }
        Err(pg_worker_core::git::GitError::Failed { code: Some(1), .. }) => {}
        Err(e) => return Err(branch_git_error(e)),
    }
    let zero = "0000000000000000000000000000000000000000";
    match run(
        repo,
        &["update-ref", branch_ref.as_str(), base_head.as_str(), zero],
    ) {
        Ok(_) => Ok(PhaseBranchResult {
            head: base_head,
            created: true,
        }),
        Err(_) => {
            let head = run(repo, &["rev-parse", branch_ref.as_str()]).map_err(branch_git_error)?;
            let base_head =
                run(repo, &["rev-parse", base_ref.as_str()]).map_err(branch_git_error)?;
            crate::phase_ancestry::ensure(repo, &base_head, &head)
                .map_err(BranchError::Ancestry)?;
            Ok(PhaseBranchResult {
                head,
                created: false,
            })
        }
    }
}

pub(super) fn branch_git_error(error: pg_worker_core::git::GitError) -> BranchError {
    match error {
        pg_worker_core::git::GitError::Io(_) => BranchError::Io,
        _ => BranchError::Refusal("phase branch git command refused".into()),
    }
}

pub(super) fn phase_branch_error(error: BranchError) -> ActivityError {
    match error {
        BranchError::Refusal(message) => {
            ActivityError::application(temporalio_sdk::ApplicationFailure::non_retryable(message))
        }
        error => activity_error(error.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_base_ref_is_a_non_retryable_refusal() {
        let dir = tempfile::tempdir().unwrap();
        pg_worker_core::git::git(dir.path(), &["init", "-q"]).unwrap();
        let input = PhaseBranchInput {
            repo: dir.path().to_str().unwrap().into(),
            base_branch: "nope".into(),
            branch: "phase/test".into(),
        };
        let error = do_ensure_phase_branch(&input).unwrap_err();
        assert_eq!(error.to_string(), "phase branch git command refused");
        let ActivityError::Application(failure) = phase_branch_error(error) else {
            panic!("expected application failure");
        };
        assert!(failure.is_non_retryable());
    }

    #[test]
    fn failed_git_commands_are_sanitized_non_retryable_refusals() {
        for code in [Some(1), Some(128), None] {
            let error = branch_git_error(pg_worker_core::git::GitError::Failed {
                args: "private args".into(),
                code,
                stderr: "private diagnostic".into(),
            });
            assert_eq!(error.to_string(), "phase branch git command refused");
            let ActivityError::Application(failure) = phase_branch_error(error) else {
                panic!("expected application failure");
            };
            assert!(failure.is_non_retryable());
        }
    }

    #[test]
    fn phase_branch_io_retries_but_refusal_does_not() {
        for error in [
            BranchError::Io,
            BranchError::Ancestry("phase branch ancestry check failed".into()),
        ] {
            if let ActivityError::Application(f) = phase_branch_error(error) {
                assert!(!f.is_non_retryable());
            }
        }
        let ActivityError::Application(f) =
            phase_branch_error(BranchError::Refusal("not a phase/ branch".into()))
        else {
            panic!("expected refusal");
        };
        assert!(f.is_non_retryable());
        assert_eq!(
            branch_git_error(pg_worker_core::git::GitError::Io(std::io::Error::other(
                "private diagnostic"
            )))
            .to_string(),
            "phase branch IO failed"
        );
    }

    #[test]
    fn ancestry_refusal_from_runner_is_retryable() {
        let dir = tempfile::tempdir().unwrap();
        let input = PhaseBranchInput {
            repo: dir.path().to_str().unwrap().into(),
            base_branch: "main".into(),
            branch: "phase/test".into(),
        };
        // Invalid object IDs force the real ancestry subprocess to fail, not
        // a hand-written approximation of its diagnostic.
        let error =
            ensure_phase_branch_with(&input, |_, _| Ok("invalid-object".into())).unwrap_err();
        let ActivityError::Application(failure) = phase_branch_error(error) else {
            panic!("expected application failure");
        };
        assert!(!failure.is_non_retryable());
    }
}
