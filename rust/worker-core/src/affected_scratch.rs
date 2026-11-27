//! Reclaim interrupted tools/pgtest.py selector scratch under its run lease.
use crate::{
    cargo_cache::CacheLease,
    disk_hygiene::{scan, Guards, Report},
    idle_targets::discovery_probe,
};
use std::{
    fs::{self, OpenOptions},
    io::{self, Read},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
    time::{Duration, SystemTime},
};

const MARKER: &[u8] = b"plangraph-affected-pytest-v1\n";

pub(crate) fn is_run(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("pg-affected-"))
}

pub(crate) fn acquire(
    root: &Path,
    guards: &Guards,
    report: &mut Report,
) -> io::Result<Result<(CacheLease, fs::Metadata), &'static str>> {
    let meta = fs::symlink_metadata(root)?;
    if !meta.is_dir()
        || meta.mode() & 0o777 != 0o700
        || !crate::private_targets::owned(root)?
        || root.canonicalize()? != root
        || guards.protects(root)
    {
        return Ok(Err("unsafe"));
    }
    let path = root.join(".lease");
    let opened = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&path);
    let lease = match opened {
        Ok(file) => file,
        Err(error) if matches!(error.raw_os_error(), Some(libc::ELOOP | libc::ENOENT)) => {
            return Ok(Err("unrecognized"))
        }
        Err(error) => return Err(error),
    };
    let lease_meta = lease.metadata()?;
    if !lease_meta.is_file() || lease_meta.uid() != meta.uid() || lease_meta.nlink() != 1 {
        return Ok(Err("unrecognized"));
    }
    let Some(lease) = CacheLease::try_exclusive_file(lease)? else {
        report
            .skipped
            .push(format!("{}: pytest lease held", root.display()));
        return Ok(Err("leased"));
    };
    let mut marker = Vec::new();
    lease
        .file()
        .take(MARKER.len() as u64 + 1)
        .read_to_end(&mut marker)?;
    if marker != MARKER || crate::reap_journal::check_mounts(root).is_err() {
        return Ok(Err("unrecognized"));
    }
    Ok(Ok((lease, meta)))
}

pub(crate) fn discover(
    root: &Path,
    excluded: &[std::path::PathBuf],
    permit: &mut impl FnMut() -> bool,
    cutoff: SystemTime,
    marked: impl Fn(&Path) -> bool,
    mut keep: impl FnMut(&Path, &'static str),
) -> io::Result<Vec<crate::disk_hygiene::Candidate>> {
    let mut found = Vec::new();
    let entries = discovery_probe(fs::read_dir(root), root, &mut keep)?;
    for entry in entries.into_iter().flatten().take_while(|_| permit()) {
        let Some(entry) = discovery_probe(entry, root, &mut keep)? else {
            continue;
        };
        let path = entry.path();
        if marked(&path) && !excluded.iter().any(|other| path.starts_with(other)) {
            let item =
                crate::disk_hygiene::Candidate::new(path.clone(), root.to_path_buf(), cutoff);
            if let Some(mut item) = discovery_probe(item, &path, &mut keep)? {
                item.selector_scratch = is_run(&path);
                found.push(item);
            }
        }
    }
    Ok(found)
}

pub(crate) fn reclaim(
    root: &Path,
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    let Ok((lease, meta)) = acquire(root, guards, report)? else {
        return Ok(());
    };
    let path = root.join(".lease");
    let lease_meta = lease.file().metadata()?;
    let (bytes, newest) = scan(root)?;
    if now.duration_since(newest).unwrap_or_default() <= Duration::from_secs(1800) {
        return Ok(());
    }
    let current = fs::symlink_metadata(root)?;
    let current_lease = fs::symlink_metadata(&path)?;
    if (current.dev(), current.ino()) != (meta.dev(), meta.ino())
        || (current_lease.dev(), current_lease.ino()) != (lease_meta.dev(), lease_meta.ino())
    {
        return Ok(());
    }
    if apply {
        fs::remove_dir_all(root)?;
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
    }
    report.actions.push(format!(
        "{} {} bytes={bytes}",
        if apply { "removed" } else { "would-remove" },
        root.display()
    ));
    Ok(())
}

#[cfg(target_os = "linux")]
impl crate::disk_hygiene::HygieneReport {
    pub fn keep_boundary(&mut self, path: std::path::PathBuf, reason: &'static str) {
        self.summary.alarm |= reason == "root_unavailable";
        let device = path
            .metadata()
            .ok()
            .map(|meta| crate::disk_hygiene::file_identity(&meta).0);
        self.kept.push(crate::disk_hygiene::KeptPath {
            device,
            path,
            bytes: None,
            bytes_is_lower_bound: false,
            reason,
        });
    }
}
