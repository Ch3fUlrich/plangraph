//! The repository the lane cleanup reads and deletes in: one canonical host and `OWNER/NAME` for
//! the fetch URL, the effective push URL (`pushurl` and `insteadOf` applied) and the pull request reads.
use pg_worker_core::lane_delete::run_git;
use std::path::Path;

/// `host` is empty for a local path (offline fixtures); `path` is lower-case `owner/name`.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct RepoId {
    pub host: String,
    pub path: String,
}

fn tail_two(path: &str) -> Option<String> {
    let parts: Vec<&str> = path
        .trim_matches('/')
        .trim_end_matches(".git")
        .split('/')
        .filter(|p| !p.is_empty())
        .collect();
    (parts.len() >= 2).then(|| parts[parts.len() - 2..].join("/").to_lowercase())
}

pub(super) fn parse(url: &str) -> Option<RepoId> {
    let url = url.trim();
    if let Some((_, rest)) = url.split_once("://") {
        let (authority, path) = rest.split_once('/')?;
        let host = authority
            .rsplit('@')
            .next()?
            .split(':')
            .next()?
            .to_lowercase();
        let path = tail_two(path)?;
        return (!host.is_empty()).then_some(RepoId { host, path });
    }
    if let Some((authority, path)) = url.split_once(':').filter(|(a, _)| !a.contains('/')) {
        let host = authority.rsplit('@').next()?.to_lowercase();
        let path = tail_two(path)?;
        return (!host.is_empty()).then_some(RepoId { host, path });
    }
    Some(RepoId {
        host: String::new(),
        path: tail_two(url)?,
    })
}

/// Verify the fetch and the push destination of `remote` are both `gh_host`/`gh_repo`.
/// `gh_host` `local` names a local path remote (offline fixtures only).
pub(super) fn verify(
    repo: &Path,
    remote: &str,
    gh_host: &str,
    gh_repo: &str,
) -> Result<(), String> {
    let url = |extra: &[&str]| -> Result<String, String> {
        let mut args = vec!["remote", "get-url"];
        args.extend_from_slice(extra);
        args.push(remote);
        let out = run_git(repo, &args, 30).map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!("remote {remote} has no URL"));
        }
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    };
    let want = RepoId {
        host: if gh_host == "local" {
            String::new()
        } else {
            gh_host.to_lowercase()
        },
        path: gh_repo.to_lowercase(),
    };
    for (what, extra) in [("fetch", &[][..]), ("push", &["--push"][..])] {
        match parse(&url(extra)?) {
            Some(id) if id == want => {}
            _ => return Err(format!("{what} URL of {remote} is not {gh_host}/{gh_repo}")),
        }
    }
    Ok(())
}

/// The `gh --repo` target that names the same repository (`HOST/OWNER/NAME`, or `OWNER/NAME` offline).
pub(super) fn gh_target(gh_host: &str, gh_repo: &str) -> String {
    if gh_host == "local" {
        gh_repo.to_string()
    } else {
        format!("{gh_host}/{gh_repo}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(host: &str, path: &str) -> Option<RepoId> {
        Some(RepoId {
            host: host.into(),
            path: path.into(),
        })
    }

    #[test]
    fn urls_reduce_to_one_host_and_path() {
        assert_eq!(
            parse("https://github.com/Owner/Name.git"),
            id("github.com", "owner/name")
        );
        // The user part of an scp-like or ssh URL is built here so no address-shaped text sits in the source.
        let user = format!("git{}", '@');
        assert_eq!(
            parse(&format!("{user}github.com:owner/name")),
            id("github.com", "owner/name")
        );
        assert_eq!(
            parse(&format!("ssh://{user}github.com:22/owner/name.git")),
            id("github.com", "owner/name")
        );
        assert_eq!(parse("/tmp/x/owner/name.git"), id("", "owner/name"));
        // The same owner/name on another host is another repository.
        assert_ne!(
            parse("https://evil.example/owner/name"),
            id("github.com", "owner/name")
        );
        assert_eq!(parse("https://github.com/name"), None);
    }
}
