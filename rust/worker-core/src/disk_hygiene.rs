//! Conservative cleanup of registered worktrees under an explicit root.
use crate::host_command;
use serde::Serialize;
use std::{
    fs, io,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime},
};

#[derive(Default)]
pub struct Guards {
    pub pinned: Vec<PathBuf>,
    pub live: Vec<PathBuf>,
}
#[derive(Debug, Serialize)]
pub struct Report {
    pub free_bytes: u64,
    pub alarm: bool,
    pub dry_run: bool,
    pub freed_bytes: u64,
    pub actions: Vec<String>,
    pub skipped: Vec<String>,
    pub error: Option<String>,
}
/// Absolute byte thresholds; the alarm matches the spawner floor in GiB.
#[derive(Clone, Copy)]
pub struct Thresholds {
    pub alarm_free_bytes: u64,
    pub cleanup_free_bytes: u64,
}
impl Default for Thresholds {
    fn default() -> Self {
        Self {
            alarm_free_bytes: 5 * 1024 * 1024 * 1024,
            cleanup_free_bytes: 10_000_000_000,
        }
    }
}
impl Thresholds {
    pub fn validate(self) -> io::Result<Self> {
        if self.alarm_free_bytes == 0 || self.cleanup_free_bytes < self.alarm_free_bytes {
            return Err(io::Error::other(
                "require 0 < alarm-free-bytes <= cleanup-free-bytes",
            ));
        }
        Ok(self)
    }
}
pub fn git(repo: &Path, args: &[&str]) -> io::Result<String> {
    let bytes = host_command::output(
        Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["-c", "core.hooksPath=/dev/null"])
            .args(args),
        Duration::from_secs(30),
    )?;
    String::from_utf8(bytes).map_err(io::Error::other)
}
impl Guards {
    /// Pinned worktrees exported by md-sync plus authoritative active run records.
    /// Missing or malformed protection data aborts the check.
    pub fn load(pins: &Path, state: &Path) -> io::Result<Self> {
        let mut guards = Self {
            pinned: serde_json::from_slice(&fs::read(pins)?).map_err(io::Error::other)?,
            live: vec![],
        };
        if guards.pinned.iter().any(|p| !p.is_absolute()) {
            return Err(io::Error::other("pins must be absolute paths"));
        }
        let entries = match fs::read_dir(state.join("runs")) {
            Ok(entries) => entries,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(guards),
            Err(e) => return Err(e),
        };
        for entry in entries {
            let path = entry?.path().join("record.json");
            if !path.is_file() {
                return Err(io::Error::other("run record unavailable"));
            }
            let row: serde_json::Value =
                serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)?;
            let status = row["status"]
                .as_str()
                .ok_or_else(|| io::Error::other("run status unavailable"))?;
            let survivors = match row.get("survivors") {
                None => false,
                Some(serde_json::Value::Array(pids)) => !pids.is_empty(),
                Some(_) => return Err(io::Error::other("invalid survivor records")),
            };
            if survivors
                || ["queued", "running", "claimed", "unknown", "cancelling"].contains(&status)
            {
                let path = row["checkout"]
                    .as_str()
                    .or_else(|| row["cwd"].as_str())
                    .ok_or_else(|| io::Error::other("active run checkout unavailable"))?;
                let path = PathBuf::from(path);
                if !path.is_absolute() {
                    return Err(io::Error::other("active run checkout must be absolute"));
                }
                guards.live.push(path);
            } else if crate::run::record::Status::parse(status).is_none()
                && ![
                    "timed_out",
                    "killed",
                    "succeeded",
                    "skipped",
                    "terminated_after_answer",
                ]
                .contains(&status)
            {
                return Err(io::Error::other("unknown run status"));
            }
        }
        Ok(guards)
    }
    pub fn protects(&self, path: &Path) -> bool {
        if path.components().any(|c| {
            c.as_os_str() == "plangraph-md-sync"
                || c.as_os_str().to_string_lossy().starts_with("live-")
        }) {
            return true;
        }
        self.pinned.iter().chain(&self.live).any(|p| {
            let p = p.canonicalize().unwrap_or_else(|_| p.clone());
            path.starts_with(&p) || p.starts_with(path)
        })
    }
}
pub(crate) fn scan(path: &Path) -> io::Result<(u64, SystemTime)> {
    let meta = fs::symlink_metadata(path)?;
    let mut newest = meta.modified()?;
    let mut bytes = if meta.is_file() { meta.len() } else { 0 };
    if meta.is_dir() && !meta.file_type().is_symlink() {
        for entry in fs::read_dir(path)? {
            let (size, modified) = scan(&entry?.path())?;
            bytes = bytes.saturating_add(size);
            newest = newest.max(modified);
        }
    }
    Ok((bytes, newest))
}
fn available_or_skip<T>(result: io::Result<T>, tree: &Path, report: &mut Report) -> Option<T> {
    match result {
        Ok(value) => Some(value),
        Err(error) => {
            report.skipped.push(format!(
                "{}: filesystem unavailable: {error}",
                tree.display()
            ));
            None
        }
    }
}
// Stop at cache roots, and never walk directory symlinks.
fn collect_caches(root: &Path, path: &Path, caches: &mut Vec<PathBuf>) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let child = entry.path();
        let relative = child.strip_prefix(root).unwrap();
        let name = entry.file_name();
        if relative == Path::new("target")
            || relative == Path::new("node_modules")
            || relative == Path::new("rust/target")
            || name == "__pycache__"
            || name == ".pytest_cache"
        {
            caches.push(child);
        } else if name != ".git" && entry.file_type()?.is_dir() {
            collect_caches(root, &child, caches)?;
        }
    }
    Ok(())
}

fn clean_caches(
    path: &Path,
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    let mut caches = Vec::new();
    collect_caches(path, path, &mut caches)?;
    if caches.is_empty() {
        return Ok(());
    }
    if caches.iter().any(|p| {
        fs::symlink_metadata(p).map_or(true, |m| m.file_type().is_symlink() || !m.is_dir())
    }) {
        report
            .skipped
            .push(format!("{}: unsafe cache path", path.display()));
        return Ok(());
    }
    let mut tracked_args = vec!["--literal-pathspecs", "ls-files", "--"];
    let relative: Vec<_> = caches
        .iter()
        .map(|p| {
            p.strip_prefix(path)
                .unwrap()
                .to_str()
                .ok_or_else(|| io::Error::other("non-UTF8 cache path"))
        })
        .collect::<io::Result<_>>()?;
    tracked_args.extend(relative.iter().copied());
    if !git(path, &["status", "--porcelain=v1", "--untracked-files=all"])?
        .trim()
        .is_empty()
        || !git(path, &tracked_args)?.trim().is_empty()
    {
        report
            .skipped
            .push(format!("{}: dirty or tracked cache", path.display()));
        return Ok(());
    }
    // Enumerate only ignored non-cache data. --directory also emits untracked
    // ancestors of nested ignored caches (e.g. src/), so do not use it here.
    let exclusions: Vec<_> = relative
        .iter()
        .map(|cache| format!(":(top,literal,exclude){cache}"))
        .collect();
    let mut ignored_args = vec![
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        ".",
    ];
    ignored_args.extend(exclusions.iter().map(String::as_str));
    let ignored = git(path, &ignored_args)?;
    if !ignored.is_empty() {
        report
            .skipped
            .push(format!("{}: ignored non-cache files", path.display()));
        return Ok(());
    }
    let (_, newest) = scan(path)?;
    if now.duration_since(newest).unwrap_or_default() <= Duration::from_secs(6 * 3600) {
        return Ok(());
    }
    for cache in caches {
        if guards.protects(path)
            || !cache.canonicalize()?.starts_with(path)
            || !git(path, &["status", "--porcelain=v1", "--untracked-files=all"])?
                .trim()
                .is_empty()
        {
            continue;
        }
        let bytes = scan(&cache)?.0;
        if apply {
            fs::remove_dir_all(&cache)?;
            report.freed_bytes = report.freed_bytes.saturating_add(bytes);
        }
        report.actions.push(format!(
            "{} {} bytes={bytes}",
            if apply { "removed" } else { "would-remove" },
            cache.display()
        ));
    }
    Ok(())
}

/// Reclaims ignored rebuildable data only. Worktree removal belongs to pg-reap.
pub fn sweep(
    repo: &Path,
    root: &Path,
    guards: &Guards,
    now: SystemTime,
    free_bytes: u64,
    thresholds: Thresholds,
    apply: bool,
) -> io::Result<Report> {
    let thresholds = thresholds.validate()?;
    let root = root.canonicalize()?;
    let mut report = Report {
        free_bytes,
        alarm: free_bytes < thresholds.alarm_free_bytes,
        dry_run: !apply,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    };
    if free_bytes >= thresholds.cleanup_free_bytes {
        return Ok(report);
    }
    let listing = git(repo, &["worktree", "list", "--porcelain", "-z"])?;
    for (index, block) in listing.split("\0\0").filter(|s| !s.is_empty()).enumerate() {
        let fields: Vec<_> = block.split('\0').collect();
        let Some(original) = fields.iter().find_map(|s| s.strip_prefix("worktree ")) else {
            continue;
        };
        let original = Path::new(original);
        let Some(path) = available_or_skip(original.canonicalize(), original, &mut report) else {
            continue;
        };
        let Some(metadata) = available_or_skip(fs::symlink_metadata(original), &path, &mut report)
        else {
            continue;
        };
        if index == 0
            || path == root
            || !path.starts_with(&root)
            || guards.protects(&path)
            || fields.iter().any(|s| s.starts_with("locked"))
            || metadata.file_type().is_symlink()
        {
            report
                .skipped
                .push(format!("{}: protected", path.display()));
            continue;
        }
        if let Err(error) = clean_caches(&path, guards, now, apply, &mut report) {
            report.skipped.push(format!(
                "{}: probe/cleanup unavailable: {error}",
                path.display()
            ));
        }
    }
    Ok(report)
}
