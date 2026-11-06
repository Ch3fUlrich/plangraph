//! Branch creation and checked adoption, with sanitized infrastructure errors.
use super::*;

pub(super) fn ensure_phase_branch_with(
    input: &PhaseBranchInput,
    mut run: impl FnMut(&Path, &[&str]) -> Result<String, pg_worker_core::git::GitError>,
) -> Result<PhaseBranchResult, String> {
    if input.repo.is_empty() || input.base_branch.is_empty() || input.branch.is_empty() {
        return Err("repo, base_branch and branch are required".to_string());
    }
    check_rev(&input.base_branch).map_err(|_| "invalid base revision".to_string())?;
    check_rev(&input.branch).map_err(|_| "invalid phase revision".to_string())?;
    if !input.branch.starts_with("phase/") {
        return Err("not a phase/ branch".to_string());
    }
    if input.branch == input.base_branch {
        return Err("phase branch must not be the base branch".to_string());
    }
    let repo = Path::new(&input.repo);
    let base_ref = format!("refs/heads/{}", input.base_branch);
    let branch_ref = format!("refs/heads/{}", input.branch);
    let base_head = run(repo, &["rev-parse", base_ref.as_str()]).map_err(branch_git_error)?;
    match run(repo, &["rev-parse", "--verify", "-q", branch_ref.as_str()]) {
        Ok(head) => {
            let base_head =
                run(repo, &["rev-parse", base_ref.as_str()]).map_err(branch_git_error)?;
            crate::phase_ancestry::ensure(repo, &base_head, &head)?;
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
            crate::phase_ancestry::ensure(repo, &base_head, &head)?;
            Ok(PhaseBranchResult {
                head,
                created: false,
            })
        }
    }
}

pub(super) fn branch_git_error(_: pg_worker_core::git::GitError) -> String {
    "phase branch IO failed".to_string()
}

pub(super) fn phase_branch_error(msg: String) -> ActivityError {
    if msg == "phase branch IO failed" || msg == "phase branch ancestry check failed" {
        activity_error(msg)
    } else {
        ActivityError::application(temporalio_sdk::ApplicationFailure::non_retryable(msg))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phase_branch_io_retries_but_refusal_does_not() {
        for message in [
            "phase branch IO failed",
            "phase branch ancestry check failed",
        ] {
            if let ActivityError::Application(f) = phase_branch_error(message.into()) {
                assert!(!f.is_non_retryable());
            }
        }
        let ActivityError::Application(f) = phase_branch_error("not a phase/ branch".into()) else {
            panic!("expected refusal");
        };
        assert!(f.is_non_retryable());
        assert_eq!(
            branch_git_error(pg_worker_core::git::GitError::Io(std::io::Error::other(
                "private diagnostic"
            ))),
            "phase branch IO failed"
        );
    }
}
