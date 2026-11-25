//! The repository the lane deletes read and push in: one canonical host and `OWNER/NAME` for
//! the fetch URL, the effective push URL (`pushurl` and `insteadOf` applied) and the pull request reads.
use crate::lane_delete::run_git;
use std::path::Path;

/// `host` is empty for a local path (offline fixtures); `path` is lower-case `owner/name`.
#[derive(Debug, PartialEq, Eq)]
pub struct RepoId {
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

pub fn parse(url: &str) -> Option<RepoId> {
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

/// Every fetch URL and every push URL of `remote` (`pushurl`, `insteadOf` applied) must reduce to
/// one repository identity; returns it. A named push can reach several destinations, so all count.
pub fn destination(repo: &Path, remote: &str) -> Result<RepoId, String> {
    let urls = |extra: &[&str]| -> Result<Vec<String>, String> {
        let mut args = vec!["remote", "get-url", "--all"];
        args.extend_from_slice(extra);
        args.push(remote);
        let out = run_git(repo, &args, 30).map_err(|e| e.to_string())?;
        let text = String::from_utf8_lossy(&out.stdout).into_owned();
        let list: Vec<String> = text.lines().map(|l| l.trim().to_string()).collect();
        if !out.status.success() || list.is_empty() || list.iter().any(String::is_empty) {
            return Err(format!("remote {remote} has no URL"));
        }
        Ok(list)
    };
    let mut ids = Vec::new();
    for (what, extra) in [("fetch", &[][..]), ("push", &["--push"][..])] {
        for url in urls(extra)? {
            ids.push(
                parse(&url)
                    .ok_or_else(|| format!("{what} URL of {remote} is not a repository URL"))?,
            );
        }
    }
    let first = ids.remove(0);
    if ids.iter().any(|id| *id != first) {
        return Err(format!(
            "fetch and push URLs of {remote} name different repositories"
        ));
    }
    Ok(first)
}

impl RepoId {
    /// The `gh --repo` target of this repository (`HOST/OWNER/NAME`, or `OWNER/NAME` for a local path).
    pub fn gh_target(&self) -> String {
        if self.host.is_empty() {
            self.path.clone()
        } else {
            format!("{}/{}", self.host, self.path)
        }
    }
}

/// The `gh --repo` target for an expected `gh_host`/`gh_repo` (`local`: `OWNER/NAME`).
pub fn target(gh_host: &str, gh_repo: &str) -> String {
    if gh_host == "local" {
        gh_repo.to_string()
    } else {
        format!("{gh_host}/{gh_repo}")
    }
}

/// `remote` is the repository `gh_host`/`gh_repo`; `gh_host` `local` names a local-path remote
/// (offline fixtures only).
pub fn verify(repo: &Path, remote: &str, gh_host: &str, gh_repo: &str) -> Result<(), String> {
    let want = RepoId {
        host: if gh_host == "local" {
            String::new()
        } else {
            gh_host.to_lowercase()
        },
        path: gh_repo.to_lowercase(),
    };
    if destination(repo, remote)? == want {
        Ok(())
    } else {
        Err(format!("URLs of {remote} are not {gh_host}/{gh_repo}"))
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

    #[test]
    fn every_push_url_must_name_the_same_repository() {
        let tmp = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| {
            let out = std::process::Command::new("git")
                .current_dir(tmp.path())
                .args(args)
                .output()
                .unwrap();
            assert!(out.status.success(), "{args:?}");
        };
        git(&["init", "-q"]);
        git(&["remote", "add", "origin", "https://github.com/owner/name"]);
        git(&[
            "config",
            "--add",
            "remote.origin.pushurl",
            "https://github.com/owner/name.git",
        ]);
        assert_eq!(
            destination(tmp.path(), "origin").unwrap().gh_target(),
            "github.com/owner/name"
        );
        // A named push can reach a second destination; one that differs refuses.
        git(&[
            "config",
            "--add",
            "remote.origin.pushurl",
            "https://github.com/owner/other",
        ]);
        assert!(destination(tmp.path(), "origin").is_err());
    }
}
