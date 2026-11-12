//! Bare repository fetches and monotonic watermarks.
use pg_worker_core::{
    lockfile, run::record_io::atomic_write_private, workspace_resolver::git_command,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    process::Output,
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Debug, PartialEq, Eq)]
pub enum Alarm {
    ForcePush,
    FetchFailed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Watermark {
    pub repo: String,
    pub main_head: String,
    pub fetched_at: u64,
}

impl Watermark {
    pub fn age(&self) -> u64 {
        self.age_at(now())
    }
    pub fn is_stale(&self) -> bool {
        self.is_stale_at(now())
    }
    fn age_at(&self, now: u64) -> u64 {
        now.saturating_sub(self.fetched_at)
    }
    fn is_stale_at(&self, now: u64) -> bool {
        self.age_at(now) >= 300
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

pub struct Mirror {
    root: PathBuf,
    repos: BTreeMap<String, String>,
}
impl Mirror {
    /// Read a JSON object mapping safe, single-component repo names to remote URLs.
    pub fn from_repos(path: &Path, root: &Path) -> io::Result<Self> {
        let repos: BTreeMap<String, String> = serde_json::from_slice(&fs::read(path)?)?;
        for (name, remote) in &repos {
            if !name
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
                || remote.trim().is_empty()
                || remote.starts_with('-')
                || remote.contains(['\n', '\r', '\0'])
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "invalid repository entry",
                ));
            }
        }
        Ok(Self {
            root: std::path::absolute(root)?,
            repos,
        })
    }
    pub fn mirror_dir(&self, repo: &str) -> io::Result<PathBuf> {
        if !self.repos.contains_key(repo) {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "unknown repository",
            ));
        }
        Ok(self.root.join(repo).join("mirror.git"))
    }
    pub fn watermark(&self, repo: &str) -> io::Result<Option<Watermark>> {
        let path = self.mirror_dir(repo)?.with_file_name("watermark.json");
        match fs::read(path) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }
    pub fn fetch(&self, repo: &str) -> Result<Watermark, Alarm> {
        self.fetch_at(repo, now())
    }
    fn fetch_at(&self, repo: &str, now: u64) -> Result<Watermark, Alarm> {
        let failed = |_| Alarm::FetchFailed;
        let dir = self.mirror_dir(repo).map_err(failed)?;
        let parent = dir.parent().unwrap();
        fs::create_dir_all(parent).map_err(failed)?;
        let _lock = lockfile::try_lock(&parent.join("fetch.lock"))
            .map_err(|_| Alarm::FetchFailed)?
            .ok_or(Alarm::FetchFailed)?;
        let old = self.watermark(repo).map_err(failed)?;
        if !dir.exists() {
            let staging = tempfile::tempdir_in(parent).map_err(failed)?;
            let clone = staging.path().join("mirror.git");
            checked(git(
                parent,
                &[
                    "clone",
                    "--bare",
                    "--",
                    &self.repos[repo],
                    clone.to_str().ok_or(Alarm::FetchFailed)?,
                ],
            )?)?;
            // Do not publish an empty clone that cannot supply an ancestry baseline.
            checked(git(
                &clone,
                &["rev-parse", "--verify", "refs/heads/main^{commit}"],
            )?)?;
            fs::rename(clone, &dir).map_err(failed)?;
        }
        // A missing watermark must not erase the mirror's accepted history.
        // Refuse an existing mirror without main rather than guess a baseline.
        let baseline = match &old {
            Some(watermark) => watermark.main_head.clone(),
            None => {
                let output = checked(git(
                    &dir,
                    &["rev-parse", "--verify", "refs/heads/main^{commit}"],
                )?)?;
                String::from_utf8(output.stdout)
                    .map_err(|_| Alarm::FetchFailed)?
                    .trim()
                    .to_owned()
            }
        };
        // A private candidate ref lets us inspect rewrites without moving accepted main.
        checked(git(
            &dir,
            &[
                "fetch",
                "--prune",
                "--no-tags",
                "--",
                &self.repos[repo],
                "+refs/heads/main:refs/pg-ctx/main",
            ],
        )?)?;
        let output = checked(git(
            &dir,
            &["rev-parse", "--verify", "refs/pg-ctx/main^{commit}"],
        )?)?;
        let head = String::from_utf8(output.stdout)
            .map_err(|_| Alarm::FetchFailed)?
            .trim()
            .to_owned();
        let ancestry = git(&dir, &["merge-base", "--is-ancestor", &baseline, &head])?;
        match ancestry.status.code() {
            Some(0) => {}
            Some(1) => return Err(Alarm::ForcePush),
            _ => return Err(Alarm::FetchFailed),
        }
        checked(git(&dir, &["update-ref", "refs/heads/main", &head])?)?;
        let watermark = Watermark {
            repo: repo.into(),
            main_head: head,
            fetched_at: now.max(old.map_or(0, |w| w.fetched_at)),
        };
        let bytes = serde_json::to_vec(&watermark).map_err(|_| Alarm::FetchFailed)?;
        atomic_write_private(&parent.join("watermark.json"), &bytes).map_err(failed)?;
        Ok(watermark)
    }
}

fn git(dir: &Path, args: &[&str]) -> Result<Output, Alarm> {
    let mut command = git_command(dir);
    command.env("GIT_CONFIG_NOSYSTEM", "1").env(
        "GIT_CONFIG_GLOBAL",
        if cfg!(windows) { "NUL" } else { "/dev/null" },
    );
    command.args(args).output().map_err(|_| Alarm::FetchFailed)
}

fn checked(output: Output) -> Result<Output, Alarm> {
    if output.status.success() {
        Ok(output)
    } else {
        Err(Alarm::FetchFailed)
    }
}

#[cfg(test)]
#[path = "mirror_tests.rs"]
mod tests;
