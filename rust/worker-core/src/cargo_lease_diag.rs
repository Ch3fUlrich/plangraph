//! The shared Cargo lease's lock file, its live holders and the refusal text that names them.
use std::{
    fs::{File, OpenOptions},
    io,
    path::{Path, PathBuf},
};

/// The lock file beside a cache root, `<root>.plangraph-lock`.
///
/// A symlinked target is named by its real directory, the path the native shim and the host
/// reclaim lease, so every route to one target contends on one lock.
pub fn lease_file(target: &Path) -> Option<PathBuf> {
    let target = target
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    let mut name = target.file_name()?.to_os_string();
    name.push(".plangraph-lock");
    Some(target.parent()?.join(name))
}

/// Pids that hold a flock on the root's lease file now, read from the kernel's lock table so a
/// dead process never shows and a cancelled one leaves nothing behind. Empty when unreadable.
#[cfg(target_os = "linux")]
pub fn lease_holders(target: &Path) -> Vec<u32> {
    use std::os::unix::fs::MetadataExt;
    let Some(meta) = lease_file(target).and_then(|file| std::fs::metadata(file).ok()) else {
        return vec![];
    };
    let table = std::fs::read_to_string("/proc/locks").unwrap_or_default();
    holders_in(
        &table,
        libc::major(meta.dev()),
        libc::minor(meta.dev()),
        meta.ino(),
    )
}

#[cfg(target_os = "linux")]
fn holders_in(table: &str, major: u32, minor: u32, inode: u64) -> Vec<u32> {
    let mut pids: Vec<u32> = table
        .lines()
        .filter_map(|line| {
            let fields: Vec<_> = line.split_whitespace().collect();
            let (kind, pid, id) = (fields.get(1)?, fields.get(4)?, fields.get(5)?);
            let mut id = id.split(':');
            let (maj, min, ino) = (id.next()?, id.next()?, id.next()?);
            (*kind == "FLOCK"
                && u32::from_str_radix(maj, 16).ok()? == major
                && u32::from_str_radix(min, 16).ok()? == minor
                && ino.parse::<u64>().ok()? == inode)
                .then(|| pid.parse().ok())
                .flatten()
        })
        .collect();
    pids.sort_unstable();
    pids.dedup();
    pids
}

/// Holders are unnamed where the kernel offers no lock table.
#[cfg(not(target_os = "linux"))]
pub fn describe_holders(_: &Path) -> String {
    String::new()
}

/// ` (held by pid 12, 34)` for refusals, or an empty string when no holder can be named.
#[cfg(target_os = "linux")]
pub fn describe_holders(target: &Path) -> String {
    let pids = lease_holders(target);
    if pids.is_empty() {
        return String::new();
    }
    let list: Vec<_> = pids.iter().map(u32::to_string).collect();
    format!(" (held by pid {})", list.join(", "))
}

#[cfg(unix)]
pub(crate) fn create_lease_file(path: &Path) -> io::Result<File> {
    OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(path)
}

/// Create the lock file beside `target` from the host, before a sandboxed child needs it: the
/// child takes shared leases on it read-only.
#[cfg(unix)]
pub fn ensure_lease_file(target: &Path) -> io::Result<PathBuf> {
    let path = lease_file(target).ok_or_else(|| io::Error::other("cache root needs a name"))?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    create_lease_file(&path)?;
    Ok(path)
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use crate::cargo_cache::{try_exclusive_lease, writer_lease};
    #[test]
    #[cfg(target_os = "linux")]
    fn lease_holders_name_the_live_pid_and_forget_it_on_release() {
        let parent = tempfile::tempdir().unwrap();
        let cache = parent.path().join("target");
        assert!(lease_holders(&cache).is_empty());
        let lease = writer_lease(&cache).unwrap();
        assert!(lease_holders(&cache).contains(&std::process::id()));
        let refused = try_exclusive_lease(&cache).unwrap();
        assert!(refused.is_none());
        assert!(describe_holders(&cache).contains(&format!("{}", std::process::id())));
        drop(lease);
        assert!(!lease_holders(&cache).contains(&std::process::id()));
    }
    #[test]
    #[cfg(target_os = "linux")]
    fn holder_table_rows_of_other_files_and_kinds_are_ignored() {
        let table = "1: FLOCK  ADVISORY  READ 77 08:01:500 0 EOF\n\
                     2: FLOCK  ADVISORY  WRITE 78 08:01:501 0 EOF\n\
                     3: POSIX  ADVISORY  WRITE 79 08:01:500 0 EOF\n\
                     4: FLOCK  ADVISORY  READ 77 08:01:500 0 EOF\n\
                     5: FLOCK  ADVISORY  READ 80 08:02:500 0 EOF\n";
        assert_eq!(holders_in(table, 8, 1, 500), vec![77]);
    }
}
