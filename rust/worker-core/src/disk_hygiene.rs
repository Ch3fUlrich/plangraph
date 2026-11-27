//! Conservative cleanup of registered worktrees under an explicit root.
#[path = "disk_fast_worktrees.rs"]
mod fast_worktrees;
#[cfg(target_os = "linux")]
#[path = "disk_hygiene_plan.rs"]
mod plan;
#[cfg(target_os = "linux")]
pub(crate) use hygiene_report::display_path;
#[cfg(target_os = "linux")]
pub use hygiene_report::{Device, HygieneReport, KeptPath};
#[cfg(target_os = "linux")]
pub use plan::{
    rank_candidates, reclaim, refresh_ranking, Budget, Candidate, FinalProof, Layout, Ranked,
    ReclaimChecks,
};

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
#[cfg(target_os = "linux")]
pub use inventory::HygieneInventory;

#[cfg(target_os = "linux")]
pub(crate) use crate::main_target::file_identity;

#[cfg(target_os = "linux")]
pub(crate) fn invalid_inventory() -> io::Error {
    io::Error::other("unusable hygiene process inventory")
}

#[cfg(target_os = "linux")]
mod inventory {
    use super::*;
    use std::{collections::BTreeSet, ffi::OsStr, os::unix::ffi::OsStrExt};

    /// Versioned process paths. Byte arrays preserve paths that are not UTF-8.
    #[derive(Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
    pub struct HygieneInventory {
        version: u8,
        pub(crate) cwds: BTreeSet<Vec<u8>>,
        pub(crate) open_paths: BTreeSet<Vec<u8>>,
        pub(crate) cargo_targets: BTreeSet<Vec<u8>>,
    }

    fn paths(bytes: &BTreeSet<Vec<u8>>) -> impl Iterator<Item = &Path> {
        bytes
            .iter()
            .map(|bytes| Path::new(OsStr::from_bytes(bytes)))
    }

    impl Default for HygieneInventory {
        fn default() -> Self {
            Self {
                version: 1,
                cwds: BTreeSet::new(),
                open_paths: BTreeSet::new(),
                cargo_targets: BTreeSet::new(),
            }
        }
    }

    impl HygieneInventory {
        pub(crate) fn merge(&mut self, mut row: Self) {
            self.cwds.append(&mut row.cwds);
            self.open_paths.append(&mut row.open_paths);
            self.cargo_targets.append(&mut row.cargo_targets);
        }

        pub(crate) fn decode(bytes: &[u8]) -> io::Result<Self> {
            let rows: Self = serde_json::from_slice(bytes).map_err(|_| invalid_inventory())?;
            if rows.version != 1
                || rows.references().any(|path| {
                    !path.is_absolute()
                        || path.as_os_str().as_bytes().contains(&0)
                        || path
                            .components()
                            .any(|part| part == std::path::Component::ParentDir)
                })
            {
                return Err(invalid_inventory());
            }
            Ok(rows)
        }

        fn references(&self) -> impl Iterator<Item = &Path> {
            paths(&self.cwds)
                .chain(paths(&self.open_paths))
                .chain(self.cargo_targets())
        }

        /// Explicit Cargo allocation roots observed in process environments.
        pub fn cargo_targets(&self) -> impl Iterator<Item = &Path> {
            paths(&self.cargo_targets)
        }

        /// A path reference protects the scope that contains it.
        pub fn blocks(&self, scope: &Path) -> bool {
            self.blocks_except(scope, Path::new(""))
        }

        pub(crate) fn blocks_except(&self, scope: &Path, lease: &Path) -> bool {
            paths(&self.cwds)
                .chain(self.cargo_targets())
                .chain(paths(&self.open_paths).filter(|path| *path != lease))
                .any(|path| path.starts_with(scope))
        }
    }
}

#[derive(Debug, Default, Serialize)]
pub struct Report {
    pub free_bytes: u64,
    pub alarm: bool,
    pub dry_run: bool,
    pub freed_bytes: u64,
    pub actions: Vec<String>,
    pub skipped: Vec<String>,
    pub error: Option<String>,
    /// Removals a dry run has named so far: a later pass over the same bytes counts them once.
    #[serde(skip)]
    pub projected: Projection,
}

#[cfg(target_os = "linux")]
mod hygiene_report {
    use super::*;
    use serde::Serializer;
    use std::{collections::BTreeMap, fmt::Write as _, os::unix::ffi::OsStrExt};

    #[derive(Debug, Serialize)]
    pub struct Device {
        pub device: u64,
        pub start_free_bytes: Option<u64>,
        pub end_free_bytes: Option<u64>,
        pub alarm_floor_bytes: u64,
        pub cleanup_floor_bytes: u64,
        pub freed_bytes: u64,
        pub projected_bytes: u64,
        #[serde(skip)]
        pub root: PathBuf,
        #[serde(skip)]
        pub(super) observed_at: u64,
    }

    impl Device {
        /// Start a ledger for one device and retain the boundary used to sample it.
        pub fn new(device: u64, root: PathBuf, available: Option<u64>, limits: Thresholds) -> Self {
            Self {
                device,
                start_free_bytes: available,
                end_free_bytes: None,
                alarm_floor_bytes: limits.alarm_free_bytes,
                cleanup_floor_bytes: limits.cleanup_free_bytes,
                freed_bytes: 0,
                projected_bytes: 0,
                root,
                observed_at: 0,
            }
        }
    }

    #[derive(Debug, Serialize)]
    pub struct KeptPath {
        pub device: Option<u64>,
        #[serde(serialize_with = "serialize_path")]
        pub path: PathBuf,
        pub bytes: Option<u64>,
        pub bytes_is_lower_bound: bool,
        pub reason: &'static str,
    }

    pub(crate) fn display_path(path: &Path) -> String {
        let mut display = String::new();
        for chunk in path.as_os_str().as_bytes().utf8_chunks() {
            display.push_str(&chunk.valid().replace('%', "%25"));
            for byte in chunk.invalid() {
                write!(display, "%{byte:02X}").unwrap();
            }
        }
        display
    }

    fn serialize_path<S: Serializer>(path: &Path, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&display_path(path))
    }

    #[derive(Debug, Default)]
    pub struct HygieneReport {
        pub summary: Report,
        pub devices: BTreeMap<u64, Device>,
        pub kept: Vec<KeptPath>,
        pub started_at: Duration,
        pub time_to_first_removal: Option<f64>,
        pub first_removal_deadline_missed: bool,
        pub stage_durations: BTreeMap<&'static str, f64>,
        pub budget_exhausted: bool,
    }

    impl HygieneReport {
        /// Begin a hygiene report with an absolute timestamp and an empty device ledger.
        pub fn new(started_at: Duration, dry_run: bool) -> Self {
            Self {
                summary: Report {
                    dry_run,
                    ..Report::default()
                },
                started_at,
                ..Self::default()
            }
        }

        pub(crate) fn report_json(&self) -> io::Result<serde_json::Value> {
            use serde_json::json;
            let mut report = serde_json::to_value(&self.summary)?;
            let mut extra = json!({
                "free_bytes": self.devices.values().filter_map(|row| row.end_free_bytes).min(), "devices": self.devices.values().collect::<Vec<_>>(), "kept": self.kept,
                "started_at": self.started_at.as_secs(), "time_to_first_removal": self.time_to_first_removal,
                "first_removal_deadline_missed": self.first_removal_deadline_missed,
                "stage_durations": self.stage_durations, "budget_exhausted": self.budget_exhausted
            });
            report
                .as_object_mut()
                .unwrap()
                .append(extra.as_object_mut().unwrap());
            Ok(report)
        }

        /// Record a device's final sample. A failed sample contributes no available bytes.
        pub fn sample_end(&mut self, device: u64, free: io::Result<u64>, observed_at: u64) {
            let row = self.devices.get_mut(&device).expect("registered device");
            row.observed_at = observed_at;
            row.end_free_bytes = free
                .inspect_err(|error| {
                    self.summary.alarm = true;
                    self.summary.error.get_or_insert_with(|| {
                        format!("device {device} final free space unavailable: {error}")
                    });
                })
                .ok();
        }
    }
}

/// Paths a dry run would remove, with the bytes credited for each. Nothing is removed, so a
/// second pass over the same tree must not count a subtree again.
#[derive(Debug, Default)]
pub struct Projection(Vec<(PathBuf, u64)>);

impl Projection {
    /// Whether `path` is, or lies under, a path already projected for removal.
    pub fn covers(&self, path: &Path) -> bool {
        self.0.iter().any(|(done, _)| path.starts_with(done))
    }

    /// Bytes already credited for removals at or under `path`.
    pub fn under(&self, path: &Path) -> u64 {
        self.0
            .iter()
            .filter(|(done, _)| done.starts_with(path))
            .fold(0, |sum, (_, bytes)| sum.saturating_add(*bytes))
    }

    /// Records the removal of `path` (`bytes` including everything under it) and returns the
    /// bytes it adds to the credit: none when it is covered, and not those of projected
    /// descendants, which the ancestor now replaces.
    pub fn project(&mut self, path: &Path, bytes: u64) -> u64 {
        if self.covers(path) {
            return 0;
        }
        let inside = self.under(path);
        self.0.retain(|(done, _)| !done.starts_with(path));
        self.0.push((path.to_path_buf(), bytes));
        bytes.saturating_sub(inside)
    }
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

    /// Trigger at the absolute thresholds or when 75%/80% of the filesystem is used.
    pub fn for_filesystem(self, total: u64) -> Self {
        let free_threshold = |percent| (u128::from(total) * percent / 100 + 1) as u64;
        Self {
            alarm_free_bytes: self.alarm_free_bytes.max(free_threshold(25)),
            cleanup_free_bytes: self.cleanup_free_bytes.max(free_threshold(20)),
        }
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
            if !crate::sweep_runs::finished(&row)? {
                let paths = crate::sweep_runs::paths(&row)?;
                if paths.is_empty() {
                    return Err(io::Error::other("active run checkout unavailable"));
                }
                guards.live.extend(paths);
            }
        }
        Ok(guards)
    }
    pub fn protected_name(path: &Path) -> bool {
        path.components().any(|c| {
            c.as_os_str() == "plangraph-md-sync"
                || c.as_os_str().to_string_lossy().starts_with("live-")
        })
    }
    /// An unfinished run owns `path`: a recorded checkout or attempt is the path or sits inside it.
    /// A run whose checkout is an ancestor (the repository root) owns nothing below it.
    pub fn live_run_owns(&self, path: &Path) -> bool {
        self.live.iter().any(|p| {
            let p = p.canonicalize().unwrap_or_else(|_| p.clone());
            p.starts_with(path)
        })
    }
    /// A reserved name, a pin on `path` or a pin nested in it protects it. Pins are not runs.
    pub fn pin_protects(&self, path: &Path) -> bool {
        Self::protected_name(path)
            || self.pinned.iter().any(|p| {
                let p = p.canonicalize().unwrap_or_else(|_| p.clone());
                path.starts_with(&p) || p.starts_with(path)
            })
    }
    /// Broad protection for deletion: `pin_protects` plus any run overlapping `path` in either
    /// direction, so classification and deletion share one pin rule.
    pub fn protects(&self, path: &Path) -> bool {
        self.pin_protects(path)
            || self.live.iter().any(|p| {
                let p = p.canonicalize().unwrap_or_else(|_| p.clone());
                path.starts_with(&p) || p.starts_with(path)
            })
    }
}
pub fn scan(path: &Path) -> io::Result<(u64, SystemTime)> {
    scan_until(path, &mut |_| true)
}
pub(crate) fn scan_until(
    path: &Path,
    visit: &mut impl FnMut(u64) -> bool,
) -> io::Result<(u64, SystemTime)> {
    if !visit(0) {
        return Err(io::ErrorKind::TimedOut.into());
    }
    let meta = fs::symlink_metadata(path)?;
    let mut newest = meta.modified()?;
    let mut bytes = if meta.is_file() { meta.len() } else { 0 };
    if !visit(bytes) {
        return Err(io::ErrorKind::TimedOut.into());
    }
    if meta.is_dir() && !meta.file_type().is_symlink() {
        for entry in fs::read_dir(path)? {
            let (size, modified) = scan_until(&entry?.path(), visit)?;
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

fn disposable_caches(path: &Path, report: &mut Report) -> io::Result<Option<Vec<PathBuf>>> {
    let mut caches = Vec::new();
    collect_caches(path, path, &mut caches)?;
    if caches.iter().any(|p| {
        fs::symlink_metadata(p).map_or(true, |m| m.file_type().is_symlink() || !m.is_dir())
    }) {
        report
            .skipped
            .push(format!("{}: unsafe cache path", path.display()));
        return Ok(None);
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
        || (!caches.is_empty() && !git(path, &tracked_args)?.trim().is_empty())
    {
        report
            .skipped
            .push(format!("{}: dirty or tracked cache", path.display()));
        return Ok(None);
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
        return Ok(None);
    }
    Ok(Some(caches))
}

fn clean_caches(
    path: &Path,
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    safe_to_remove: &impl Fn(&Path) -> io::Result<bool>,
) -> io::Result<()> {
    let Some(caches) = disposable_caches(path, report)? else {
        return Ok(());
    };
    if caches.is_empty() {
        return Ok(());
    }
    let (_, newest) = scan(path)?;
    if now.duration_since(newest).unwrap_or_default() <= Duration::from_secs(24 * 3600) {
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
            if !safe_to_remove(path)? {
                report
                    .skipped
                    .push(format!("{}: active work or build", path.display()));
                break;
            }
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

/// Reclaim unused caches under disk pressure and old disposable fast-check worktrees.
pub fn sweep(
    repo: &Path,
    root: &Path,
    guards: &Guards,
    now: SystemTime,
    free_bytes: u64,
    thresholds: Thresholds,
    apply: bool,
) -> io::Result<Report> {
    sweep_checked(
        repo,
        root,
        guards,
        now,
        free_bytes,
        thresholds,
        apply,
        |_| Ok(true),
    )
}

/// Recheck host protection after scanning and immediately before removing each cache.
#[allow(clippy::too_many_arguments)]
pub fn sweep_checked(
    repo: &Path,
    root: &Path,
    guards: &Guards,
    now: SystemTime,
    free_bytes: u64,
    thresholds: Thresholds,
    apply: bool,
    safe_to_remove: impl Fn(&Path) -> io::Result<bool>,
) -> io::Result<Report> {
    let root = root.canonicalize()?;
    let mut report = Report {
        free_bytes,
        alarm: free_bytes < thresholds.alarm_free_bytes,
        dry_run: !apply,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
        projected: Default::default(),
    };
    let listing = git(repo, &["worktree", "list", "--porcelain", "-z"])?;
    for (index, block) in listing.split("\0\0").filter(|s| !s.is_empty()).enumerate() {
        let fields: Vec<_> = block.split('\0').collect();
        let Some(original) = fields.iter().find_map(|s| s.strip_prefix("worktree ")) else {
            continue;
        };
        let original = Path::new(original);
        let fast = fields.contains(&"detached")
            && original
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.ends_with("-fast"));
        if !fast && free_bytes >= thresholds.cleanup_free_bytes {
            continue;
        }
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
        let result = if fast {
            fast_worktrees::remove_old_fast_tree(
                repo,
                &path,
                now,
                apply,
                &mut report,
                &safe_to_remove,
            )
        } else {
            clean_caches(&path, guards, now, apply, &mut report, &safe_to_remove)
        };
        if let Err(error) = result {
            report.skipped.push(format!(
                "{}: probe/cleanup unavailable: {error}",
                path.display()
            ));
        }
    }
    Ok(report)
}
