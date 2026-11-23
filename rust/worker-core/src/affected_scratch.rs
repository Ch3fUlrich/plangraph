//! Reclaim interrupted tools/pgtest.py selector scratch under its run lease.
use crate::disk_hygiene::{scan, Guards, Report};
use std::{
    fs::{self, OpenOptions},
    io::{self, Read},
    os::{
        fd::AsRawFd,
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::Path,
    time::{Duration, SystemTime},
};

const MARKER: &[u8] = b"plangraph-affected-pytest-v1\n";

pub(crate) fn is_run(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("pg-affected-"))
}

pub(crate) fn reclaim(
    root: &Path,
    guards: &Guards,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    let meta = fs::symlink_metadata(root)?;
    if !meta.is_dir()
        || meta.mode() & 0o777 != 0o700
        || !crate::private_targets::owned(root)?
        || root.canonicalize()? != root
        || guards.protects(root)
    {
        return Ok(());
    }
    let path = root.join(".lease");
    let opened = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&path);
    let mut lease = match opened {
        Ok(file) => file,
        Err(error) if matches!(error.raw_os_error(), Some(libc::ELOOP | libc::ENOENT)) => {
            return Ok(())
        }
        Err(error) => return Err(error),
    };
    let lease_meta = lease.metadata()?;
    if !lease_meta.is_file() || lease_meta.uid() != meta.uid() || lease_meta.nlink() != 1 {
        return Ok(());
    }
    // SAFETY: the file owns a live descriptor. Its drop releases the flock.
    if unsafe { libc::flock(lease.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::WouldBlock {
            report
                .skipped
                .push(format!("{}: pytest lease held", root.display()));
            return Ok(());
        }
        return Err(error);
    }
    let mut marker = Vec::new();
    (&mut lease)
        .take(MARKER.len() as u64 + 1)
        .read_to_end(&mut marker)?;
    if marker != MARKER || crate::reap_journal::check_mounts(root).is_err() {
        return Ok(());
    }
    let (bytes, newest) = scan(root)?;
    if SystemTime::now().duration_since(newest).unwrap_or_default() <= Duration::from_secs(1800) {
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
