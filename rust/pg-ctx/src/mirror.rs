//! Bare repository fetches and monotonic watermarks.
use pg_worker_core::{lockfile, run::record_io::atomic_write_private};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[path = "mirror_backend.rs"]
mod backend;
#[path = "mirror_worker.rs"]
mod worker;
use backend::{copy_repository, open, set_main};

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Alarm {
    ForcePush,
    FetchFailed,
    FetchTimedOut,
    FetchDeferred,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Watermark {
    pub repo: String,
    pub main_head: String,
    #[serde(default)]
    pub blob_shas: BTreeMap<String, String>,
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
        self.age_at(now) > 300
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
    fetch_worker: PathBuf,
}
impl Mirror {
    /// Read a JSON object mapping safe, single-component repo names to remote URLs.
    pub fn from_repos(path: &Path, root: &Path) -> io::Result<Self> {
        let repos: BTreeMap<String, String> = serde_json::from_slice(&fs::read(path)?)?;
        Self::from_entries(repos, root)
    }
    fn from_entries(repos: BTreeMap<String, String>, root: &Path) -> io::Result<Self> {
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
            fetch_worker: std::env::current_exe()
                .ok()
                .filter(|p| p.file_stem().is_some_and(|name| name == "pg-repo-mirror"))
                .unwrap_or_else(|| PathBuf::from("pg-repo-mirror")),
        })
    }
    /// Attempt repositories using killable native fetch workers.
    pub fn fetch_all(&self) -> BTreeMap<String, Result<Watermark, Alarm>> {
        let started = Instant::now();
        self.fetch_all_with(
            || started.elapsed(),
            |name, timeout| worker::fetch(self, name, timeout),
        )
    }
    fn fetch_all_with(
        &self,
        elapsed: impl FnMut() -> Duration,
        fetch: impl FnMut(&str, Duration) -> Result<Watermark, Alarm>,
    ) -> BTreeMap<String, Result<Watermark, Alarm>> {
        self.fetch_tick(
            Duration::from_secs(5),
            Duration::from_secs(45),
            elapsed,
            fetch,
        )
    }
    fn fetch_tick(
        &self,
        timeout: Duration,
        budget: Duration,
        mut elapsed: impl FnMut() -> Duration,
        mut fetch: impl FnMut(&str, Duration) -> Result<Watermark, Alarm>,
    ) -> BTreeMap<String, Result<Watermark, Alarm>> {
        let failed = || {
            self.repos
                .keys()
                .map(|name| (name.clone(), Err(Alarm::FetchFailed)))
                .collect()
        };
        if fs::create_dir_all(&self.root).is_err() {
            return failed();
        }
        let _lock = match lockfile::try_lock(&self.root.join(".fetch-all.lock")) {
            Ok(Some(lock)) => lock,
            _ => return failed(),
        };
        let cursor = self.root.join(".fetch-next");
        let saved = match fs::read_to_string(&cursor) {
            Ok(name) => name,
            Err(e) if e.kind() == io::ErrorKind::NotFound => String::new(),
            Err(_) => return failed(),
        };
        let names: Vec<_> = self.repos.keys().collect();
        let start = names
            .iter()
            .position(|name| name.as_str() >= saved.as_str())
            .unwrap_or(0);
        let mut results: BTreeMap<_, _> = names
            .iter()
            .map(|name| ((*name).clone(), Err(Alarm::FetchDeferred)))
            .collect();
        for offset in 0..names.len() {
            if elapsed().saturating_add(timeout) > budget {
                break;
            }
            let index = (start + offset) % names.len();
            let name = names[index];
            // Persist the successor before starting a worker so interruption cannot
            // let a repeatedly stuck repository starve its successors next tick.
            let next = names[(index + 1) % names.len()];
            if atomic_write_private(&cursor, next.as_bytes()).is_err() {
                results.insert(name.clone(), Err(Alarm::FetchFailed));
                break;
            }
            results.insert(name.clone(), fetch(name, timeout));
        }
        results
    }
    /// Library callers may pin the installed worker path explicitly.
    pub fn with_fetch_worker(mut self, worker: impl Into<PathBuf>) -> Self {
        self.fetch_worker = worker.into();
        self
    }
    pub fn fetch_all_with_timeout(
        &self,
        timeout: Duration,
    ) -> BTreeMap<String, Result<Watermark, Alarm>> {
        let started = Instant::now();
        self.fetch_tick(
            timeout,
            Duration::from_secs(45).max(timeout),
            || started.elapsed(),
            |name, timeout| worker::fetch(self, name, timeout),
        )
    }
    /// Internal worker entry point: fetches with the native backend, without recursion.
    pub fn run_fetch_worker(request: &Path, result: &Path) -> io::Result<()> {
        worker::run(request, result)
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
    /// Replace the named-file set, recording each blob SHA at the accepted main.
    /// Later `fetch` calls refresh the same set; missing/non-blob paths refuse publication.
    pub fn fetch_named(&self, repo: &str, paths: &[&str]) -> Result<Watermark, Alarm> {
        self.fetch_named_at(repo, Some(paths), now())
    }
    fn fetch_at(&self, repo: &str, now: u64) -> Result<Watermark, Alarm> {
        self.fetch_named_at(repo, None, now)
    }
    fn fetch_named_at(
        &self,
        repo: &str,
        paths: Option<&[&str]>,
        now: u64,
    ) -> Result<Watermark, Alarm> {
        let started = Instant::now();
        self.fetch_named_with_wait(repo, paths, now, || started.elapsed(), std::thread::sleep)
    }
    fn fetch_named_with_wait(
        &self,
        repo: &str,
        paths: Option<&[&str]>,
        now: u64,
        elapsed: impl FnMut() -> Duration,
        sleep: impl FnMut(Duration),
    ) -> Result<Watermark, Alarm> {
        let failed = |_| Alarm::FetchFailed;
        let accepted_dir = self.mirror_dir(repo).map_err(failed)?;
        let parent = accepted_dir.parent().unwrap();
        fs::create_dir_all(parent).map_err(failed)?;
        let _lock = fetch_lock_wait(
            || lockfile::try_lock(&parent.join("fetch.lock")).map_err(|_| Alarm::FetchFailed),
            elapsed,
            sleep,
        )?;
        // Only sweep our own abandoned quarantine directories, under the writer lock.
        for entry in fs::read_dir(parent).map_err(failed)? {
            let entry = entry.map_err(failed)?;
            if entry
                .file_name()
                .to_string_lossy()
                .starts_with(STAGING_PREFIX)
                && entry.file_type().map_err(failed)?.is_dir()
            {
                fs::remove_dir_all(entry.path()).map_err(failed)?;
            }
        }
        let old = self.watermark(repo).map_err(failed)?;
        let staging = tempfile::Builder::new()
            .prefix(STAGING_PREFIX)
            .tempdir_in(parent)
            .map_err(failed)?;
        let dir = staging.path().join("mirror.git");
        let existed = accepted_dir.exists();
        if existed {
            copy_repository(&accepted_dir, &dir).map_err(failed)?;
        } else {
            backend::init(&dir)?;
        }
        let repository = open(&dir)?;
        // Missing watermarks cannot erase accepted history. A rebuilt clone can
        // recover a watermark's missing baseline from the fetched ancestry.
        let mut baselines =
            match &old {
                Some(w) => vec![gix::ObjectId::from_hex(w.main_head.as_bytes())
                    .map_err(|_| Alarm::FetchFailed)?],
                None if existed => vec![backend::main_head(&repository)?],
                None => Vec::new(),
            };
        // Publication may have completed just before a crash writing watermark.
        // Neither that accepted main nor the persisted watermark may be rewound.
        if existed
            && old.is_some()
            && repository
                .try_find_reference("refs/heads/main")
                .map_err(|_| Alarm::FetchFailed)?
                .is_some()
        {
            let accepted = backend::main_head(&repository)?;
            if !baselines.contains(&accepted) {
                baselines.push(accepted);
            }
        }
        let head = backend::fetch(&repository, &self.repos[repo])?;
        for baseline in baselines {
            if repository.find_commit(baseline).is_err() {
                return Err(Alarm::ForcePush);
            }
            let mut found = false;
            for ancestor in repository
                .find_commit(head)
                .map_err(|_| Alarm::FetchFailed)?
                .ancestors()
                .all()
                .map_err(|_| Alarm::FetchFailed)?
            {
                if ancestor.map_err(|_| Alarm::FetchFailed)?.id == baseline {
                    found = true;
                    break;
                }
            }
            if !found {
                return Err(Alarm::ForcePush);
            }
        }
        let names = paths.map_or_else(
            || {
                old.as_ref().map_or_else(Vec::new, |w| {
                    w.blob_shas.keys().map(String::as_str).collect()
                })
            },
            |paths| paths.to_vec(),
        );
        let tree = repository
            .find_commit(head)
            .and_then(|c| c.tree())
            .map_err(|_| Alarm::FetchFailed)?;
        let mut blob_shas = BTreeMap::new();
        for name in names {
            if name.contains(['\\', '\0', '\n', '\r'])
                || name.split('/').any(|part| matches!(part, "" | "." | ".."))
            {
                return Err(Alarm::FetchFailed);
            }
            let entry = tree
                .lookup_entry_by_path(name)
                .map_err(|_| Alarm::FetchFailed)?
                .ok_or(Alarm::FetchFailed)?;
            repository
                .find_blob(entry.object_id())
                .map_err(|_| Alarm::FetchFailed)?;
            blob_shas.insert(name.into(), entry.object_id().to_string());
        }
        set_main(&repository, head)?;
        // No candidate refs survive publication, including a no-change fetch.
        if let Some(reference) = repository
            .try_find_reference("refs/pg-ctx/main")
            .map_err(|_| Alarm::FetchFailed)?
        {
            reference.delete().map_err(|_| Alarm::FetchFailed)?;
        }
        drop(tree);
        drop(repository);
        publish_with(&dir, &accepted_dir, || Ok(())).map_err(failed)?;
        let watermark = Watermark {
            repo: repo.into(),
            main_head: head.to_string(),
            blob_shas,
            fetched_at: now.max(old.map_or(0, |w| w.fetched_at)),
        };
        let bytes = serde_json::to_vec(&watermark).map_err(|_| Alarm::FetchFailed)?;
        atomic_write_private(&parent.join("watermark.json"), &bytes).map_err(failed)?;
        Ok(watermark)
    }
}

const STAGING_PREFIX: &str = ".pg-ctx-fetch-";
const FETCH_LOCK_TIMEOUT: Duration = Duration::from_secs(5);

fn fetch_lock_wait<T>(
    mut attempt: impl FnMut() -> Result<Option<T>, Alarm>,
    mut elapsed: impl FnMut() -> Duration,
    mut sleep: impl FnMut(Duration),
) -> Result<T, Alarm> {
    let mut delay = Duration::from_millis(5);
    loop {
        if let Some(lock) = attempt()? {
            return Ok(lock);
        }
        let remaining = FETCH_LOCK_TIMEOUT.saturating_sub(elapsed());
        if remaining.is_zero() {
            return Err(Alarm::FetchFailed);
        }
        sleep(delay.min(remaining));
        delay = (delay * 2).min(Duration::from_millis(100));
    }
}

fn publish_with(
    dir: &Path,
    accepted: &Path,
    after_switch: impl FnOnce() -> io::Result<()>,
) -> io::Result<()> {
    if accepted.exists() {
        // One syscall: lockless readers see old or new, never an absent mirror.
        // On failure (including unsupported filesystems) old remains accepted.
        #[cfg(target_os = "linux")]
        rustix::fs::renameat_with(
            rustix::fs::CWD,
            dir,
            rustix::fs::CWD,
            accepted,
            rustix::fs::RenameFlags::EXCHANGE,
        )?;
        #[cfg(not(target_os = "linux"))]
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "atomic directory exchange required",
        ));
    } else {
        fs::rename(dir, accepted)?;
    }
    // Failure/unwind here only drops the old generation, not accepted history.
    after_switch()
}

#[cfg(test)]
#[path = "mirror_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "mirror_schedule_tests.rs"]
mod schedule_tests;
