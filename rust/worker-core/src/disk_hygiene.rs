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
    pub disk_pct: f64,
    pub alarm: bool,
    pub dry_run: bool,
    pub freed_bytes: u64,
    pub actions: Vec<String>,
    pub skipped: Vec<String>,
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
            if ["queued", "running", "claimed", "unknown", "cancelling"].contains(&status) {
                let path = row["checkout"]
                    .as_str()
                    .or_else(|| row["cwd"].as_str())
                    .ok_or_else(|| io::Error::other("active run checkout unavailable"))?;
                let path = PathBuf::from(path);
                if !path.is_absolute() {
                    return Err(io::Error::other("active run checkout must be absolute"));
                }
                guards.live.push(path);
            } else if crate::run::record::Status::parse(status).is_none() {
                return Err(io::Error::other("unknown run status"));
            }
        }
        Ok(guards)
    }
    fn protects(&self, path: &Path) -> bool {
        self.pinned.iter().chain(&self.live).any(|p| {
            let p = p.canonicalize().unwrap_or_else(|_| p.clone());
            path.starts_with(&p) || p.starts_with(path)
        })
    }
}
fn scan(path: &Path) -> io::Result<(u64, SystemTime)> {
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
fn removable(repo: &Path, branch: &str) -> io::Result<bool> {
    let target = git(
        repo,
        &["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
    )
    .unwrap_or_else(|_| "refs/remotes/origin/main".into());
    let merged = git(
        repo,
        &[
            "for-each-ref",
            "--format=%(refname)",
            "--merged",
            target.trim(),
            "refs/heads/",
        ],
    )?;
    if merged.lines().any(|s| s == branch) {
        return Ok(true);
    }
    let Some(short) = branch.strip_prefix("refs/heads/") else {
        return Ok(false);
    };
    let remote = git(
        repo,
        &["config", "--get", &format!("branch.{short}.remote")],
    )
    .unwrap_or_default();
    let merge =
        git(repo, &["config", "--get", &format!("branch.{short}.merge")]).unwrap_or_default();
    if remote.trim() != "origin" || merge.trim() != branch {
        return Ok(false);
    }
    // Show-ref exit 1 is ambiguous with other errors: enumerate refs successfully first.
    let refs = git(
        repo,
        &[
            "for-each-ref",
            "--format=%(refname)",
            "refs/remotes/origin/",
        ],
    )?;
    Ok(!refs
        .lines()
        .any(|s| s == format!("refs/remotes/origin/{short}")))
}

pub(crate) fn has_ignored_non_cache(path: &Path) -> io::Result<bool> {
    // NUL delimiters preserve filenames containing whitespace or newlines.
    let ignored = git(
        path,
        &[
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ],
    )?;
    Ok(ignored
        .split('\0')
        .filter(|s| !s.is_empty())
        .any(|s| !s.starts_with("target/") && !s.starts_with("node_modules/")))
}

/// Caller refreshes origin with fetch --prune before applying this snapshot.
pub fn sweep(
    repo: &Path,
    root: &Path,
    guards: &Guards,
    now: SystemTime,
    disk_pct: f64,
    apply: bool,
) -> io::Result<Report> {
    if !disk_pct.is_finite() || !(0.0..=100.0).contains(&disk_pct) {
        return Err(io::Error::other("invalid disk usage"));
    }
    let root = root.canonicalize()?;
    let mut report = Report {
        disk_pct,
        alarm: disk_pct >= 75.0,
        dry_run: !apply,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
    };
    if disk_pct < 80.0 {
        return Ok(report);
    }
    let listing = git(repo, &["worktree", "list", "--porcelain", "-z"])?;
    'trees: for (index, block) in listing.split("\0\0").filter(|s| !s.is_empty()).enumerate() {
        let fields: Vec<_> = block.split('\0').collect();
        let Some(path) = fields.iter().find_map(|s| s.strip_prefix("worktree ")) else {
            continue;
        };
        let original = Path::new(path);
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
        if !git(
            &path,
            &["status", "--porcelain=v1", "--untracked-files=all"],
        )?
        .trim()
        .is_empty()
            || !git(&path, &["ls-files", "--", "target", "node_modules"])?
                .trim()
                .is_empty()
        {
            report
                .skipped
                .push(format!("{}: dirty or tracked cache", path.display()));
            continue;
        }
        if has_ignored_non_cache(&path)? {
            report
                .skipped
                .push(format!("{}: ignored non-cache files", path.display()));
            continue;
        }
        let Some((_, newest)) = available_or_skip(scan(&path), &path, &mut report) else {
            continue;
        };
        if now.duration_since(newest).unwrap_or_default() <= Duration::from_secs(86400) {
            continue;
        }
        let caches: Vec<_> = [path.join("target"), path.join("node_modules")]
            .into_iter()
            .filter(|p| p.exists() || p.is_symlink())
            .collect();
        if caches.iter().any(|p| {
            fs::symlink_metadata(p).map_or(true, |m| m.file_type().is_symlink() || !m.is_dir())
        }) {
            report
                .skipped
                .push(format!("{}: unsafe cache path", path.display()));
            continue;
        }
        // Recheck protection, dirtiness and containment immediately before each mutation.
        for cache in caches {
            let Some(cache_path) = available_or_skip(cache.canonicalize(), &path, &mut report)
            else {
                continue 'trees;
            };
            if guards.protects(&path)
                || !cache_path.starts_with(&path)
                || !git(
                    &path,
                    &["status", "--porcelain=v1", "--untracked-files=all"],
                )?
                .trim()
                .is_empty()
            {
                continue;
            }
            let Some((bytes, _)) = available_or_skip(scan(&cache), &path, &mut report) else {
                continue 'trees;
            };
            if apply {
                fs::remove_dir_all(&cache)?;
                report.freed_bytes += bytes;
            }
            report.actions.push(format!(
                "{} {} bytes={bytes}",
                if apply { "removed" } else { "would-remove" },
                cache.display()
            ));
        }
        if let Some(branch) = fields.iter().find_map(|s| s.strip_prefix("branch ")) {
            if removable(repo, branch)?
                && !guards.protects(&path)
                && git(
                    &path,
                    &["status", "--porcelain=v1", "--untracked-files=all"],
                )?
                .trim()
                .is_empty()
            {
                if has_ignored_non_cache(&path)? {
                    report
                        .skipped
                        .push(format!("{}: ignored non-cache files", path.display()));
                    continue;
                }
                if apply {
                    let Some((bytes, _)) = available_or_skip(scan(&path), &path, &mut report)
                    else {
                        continue;
                    };
                    git(
                        repo,
                        &[
                            "worktree",
                            "remove",
                            path.to_str()
                                .ok_or_else(|| io::Error::other("invalid worktree path"))?,
                        ],
                    )?;
                    report.freed_bytes += bytes;
                }
                report.actions.push(format!(
                    "{} worktree {}",
                    if apply { "removed" } else { "would-remove" },
                    path.display()
                ));
            }
        }
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vanished_scan_is_recorded_as_skipped() {
        let tree = tempfile::tempdir().unwrap();
        let file = tree.path().join("vanishing");
        fs::write(&file, "data").unwrap();
        fs::remove_file(&file).unwrap();
        let mut report = Report {
            disk_pct: 85.0,
            alarm: true,
            dry_run: false,
            freed_bytes: 0,
            actions: vec![],
            skipped: vec![],
        };
        // Exercise the same error boundary used for recursive and pre-removal scans.
        assert!(available_or_skip(scan(&file), tree.path(), &mut report).is_none());
        assert_eq!(report.skipped.len(), 1);
        assert!(report.skipped[0].contains("filesystem unavailable"));
        assert!(report.skipped[0].contains(&tree.path().display().to_string()));
        assert!(report.actions.is_empty());
    }
}
