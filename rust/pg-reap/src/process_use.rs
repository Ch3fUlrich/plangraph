//! Linux procfs cwd checks immediately before destructive worktree operations.
use std::{
    fs, io,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};
fn identity(path: &Path) -> io::Result<Option<u64>> {
    let stat = fs::read_to_string(path.join("stat"))?;
    let fields = stat
        .rsplit_once(')')
        .ok_or_else(|| io::Error::other("invalid proc stat"))?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    if matches!(fields.first(), Some(&"Z" | &"X")) {
        return Ok(None);
    }
    let start = fields
        .get(19)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| io::Error::other("invalid proc start"))?;
    Ok(Some(start))
}
pub fn tree_in_use(proc_root: &Path, tree: &Path) -> io::Result<bool> {
    scan(
        proc_root,
        tree,
        |path| Ok(fs::metadata(path)?.uid()),
        |path| fs::read_link(path),
    )
}
fn scan(
    proc_root: &Path,
    tree: &Path,
    owner: impl Fn(&Path) -> io::Result<u32>,
    cwd: impl Fn(&Path) -> io::Result<PathBuf>,
) -> io::Result<bool> {
    let mut unreadable = false;
    for entry in fs::read_dir(proc_root)? {
        let entry = entry?;
        if !entry
            .file_name()
            .to_string_lossy()
            .bytes()
            .all(|b| b.is_ascii_digit())
        {
            continue;
        }
        let sample = || -> io::Result<bool> {
            if owner(&entry.path())? != unsafe { libc::geteuid() } {
                return Ok(false);
            }
            let before = identity(&entry.path())?;
            if before.is_none() {
                return Ok(false);
            }
            let cwd = cwd(&entry.path().join("cwd"))?;
            let after = identity(&entry.path())?;
            Ok(before == after && cwd.starts_with(tree))
        };
        match sample() {
            Ok(true) => return Ok(true),
            Ok(false) => {}
            Err(e)
                if e.kind() == io::ErrorKind::NotFound || e.raw_os_error() == Some(libc::ESRCH) => {
            }
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied => unreadable = true,
            Err(e) => return Err(e),
        }
    }
    if unreadable {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "process_visibility_incomplete",
        ));
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> tempfile::TempDir {
        let root = tempfile::tempdir().unwrap();
        let pid = root.path().join("123");
        fs::create_dir(&pid).unwrap();
        let mut fields = vec!["0"; 20];
        fields[0] = "S";
        fields[19] = "42";
        fs::write(
            pid.join("stat"),
            format!("123 (fixture) {}", fields.join(" ")),
        )
        .unwrap();
        root
    }
    #[test]
    fn other_uid_unreadable_cwd_is_skipped() {
        let root = fixture();
        let other_uid = unsafe { libc::geteuid() }.wrapping_add(1);
        let result = scan(
            root.path(),
            &root.path().join("unused"),
            |_| Ok(other_uid),
            |_| Err(io::Error::from_raw_os_error(libc::EACCES)),
        );
        assert!(!result.unwrap());
    }
    #[test]
    fn same_uid_unreadable_cwd_refuses() {
        let root = fixture();
        let result = scan(
            root.path(),
            &root.path().join("unused"),
            |_| Ok(unsafe { libc::geteuid() }),
            |_| Err(io::Error::from_raw_os_error(libc::EACCES)),
        );
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
    }
    #[test]
    fn vanished_process_is_skipped_for_enoent_and_esrch() {
        let root = fixture();
        for errno in [libc::ENOENT, libc::ESRCH] {
            let result = scan(
                root.path(),
                root.path(),
                |_| Ok(unsafe { libc::geteuid() }),
                |_| Err(io::Error::from_raw_os_error(errno)),
            );
            assert!(!result.unwrap());
        }
    }
    #[test]
    fn real_procfs_unused_tree_respects_same_uid_visibility() {
        let tree = tempfile::tempdir().unwrap();
        // Some hosts also hide same-UID service/SSH processes. A success-only
        // expectation there would contradict the required same-UID refusal.
        // Observe that restriction independently on both sides of the scan.
        let denied = || {
            fs::read_dir("/proc")
                .unwrap()
                .filter_map(|entry| {
                    let path = entry.ok()?.path();
                    if fs::metadata(&path).ok()?.uid() != unsafe { libc::geteuid() } {
                        return None;
                    }
                    let start = identity(&path).ok()??;
                    let error = fs::read_link(path.join("cwd")).err()?;
                    (error.kind() == io::ErrorKind::PermissionDenied).then_some((path, start))
                })
                .collect::<std::collections::BTreeSet<_>>()
        };
        let before = denied();
        let result = tree_in_use(Path::new("/proc"), tree.path());
        let after = denied();
        if before.intersection(&after).next().is_some() {
            assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        } else {
            assert!(!result.unwrap());
        }
    }
}
