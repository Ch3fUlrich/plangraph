//! The repository the lane deletes read and push in: one canonical host and `OWNER/NAME` for
//! the fetch URL, the effective push URL (`pushurl` and `insteadOf` applied) and the pull request reads.
use crate::lane_delete::run_git;
use std::path::Path;

/// `host` is empty for a local path, whose `path` is then the canonical absolute directory (two
/// mirrors with the same `owner/name` tail are different repositories); a hosted repository's
/// `path` is its full lower-case path without `.git`.
#[derive(Debug, PartialEq, Eq)]
pub struct RepoId {
    pub host: String,
    pub path: String,
}

fn hosted_path(path: &str) -> Option<String> {
    let path = path.trim_matches('/');
    let path = path.strip_suffix(".git").unwrap_or(path);
    (path.split('/').count() >= 2 && !path.split('/').any(str::is_empty))
        .then(|| path.to_lowercase())
}

fn local(path: &str) -> Option<RepoId> {
    let raw = std::path::Path::new(path);
    let canonical = std::fs::canonicalize(raw).unwrap_or_else(|_| raw.to_path_buf());
    let text = canonical
        .to_string_lossy()
        .trim_end_matches('/')
        .to_string();
    (text.starts_with('/') || text.contains(":\\")).then(|| RepoId {
        host: String::new(),
        path: text,
    })
}

pub fn parse(url: &str) -> Option<RepoId> {
    let url = url.trim();
    if let Some(rest) = url.strip_prefix("file://") {
        return local(rest);
    }
    if let Some((_, rest)) = url.split_once("://") {
        let (authority, path) = rest.split_once('/')?;
        let host = authority
            .rsplit('@')
            .next()?
            .split(':')
            .next()?
            .to_lowercase();
        let path = hosted_path(path)?;
        return (!host.is_empty()).then_some(RepoId { host, path });
    }
    if let Some((authority, path)) = url.split_once(':').filter(|(a, _)| !a.contains('/')) {
        let host = authority.rsplit('@').next()?.to_lowercase();
        let path = hosted_path(path)?;
        return (!host.is_empty()).then_some(RepoId { host, path });
    }
    local(url)
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
            let parts: Vec<&str> = self
                .path
                .trim_end_matches(".git")
                .rsplit('/')
                .take(2)
                .collect();
            format!("{}/{}", parts.get(1).unwrap_or(&""), parts[0])
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
    let id = destination(repo, remote)?;
    let want = gh_repo.to_lowercase();
    let same = if gh_host == "local" {
        id.host.is_empty()
            && id
                .path
                .trim_end_matches(".git")
                .ends_with(&format!("/{want}"))
    } else {
        id.host == gh_host.to_lowercase() && id.path == want
    };
    if same {
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
        assert_eq!(
            parse("/tmp/x/owner/name.git"),
            id("", "/tmp/x/owner/name.git")
        );
        // Same tail, different directory: a different repository.
        assert_ne!(
            parse("/primary/owner/repo.git"),
            parse("/mirror/owner/repo.git")
        );
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
        // The same owner/repo tail in two directories is two repositories: a push to both refuses.
        let primary = tmp.path().join("primary/owner/repo.git");
        let mirror = tmp.path().join("mirror/owner/repo.git");
        std::fs::create_dir_all(&primary).unwrap();
        std::fs::create_dir_all(&mirror).unwrap();
        let (primary, mirror) = (primary.to_str().unwrap(), mirror.to_str().unwrap());
        git(&["remote", "add", "mix", primary]);
        git(&["config", "--add", "remote.mix.pushurl", primary]);
        assert!(destination(tmp.path(), "mix").is_ok());
        git(&["config", "--add", "remote.mix.pushurl", mirror]);
        assert!(destination(tmp.path(), "mix").is_err());
    }
}
