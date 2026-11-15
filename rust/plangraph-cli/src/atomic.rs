//! Same-directory atomic replacement. The caller holds a per-target lock.
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;

pub fn private_options() -> OpenOptions {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

pub fn regular(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(m) if !m.is_file() || m.file_type().is_symlink() => {
            Err(io::Error::other("target must be a regular file"))
        }
        Ok(_) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

pub fn lock(path: &Path) -> io::Result<File> {
    lock_with(path, |_| {})
}
fn lock_with(path: &Path, after_check: impl FnOnce(&Path)) -> io::Result<File> {
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing target name"))?;
    let lock_path = path.with_file_name(format!("{}.plangraph-lock", name.to_string_lossy()));
    regular(&lock_path)?;
    after_check(&lock_path);
    let mut options = private_options();
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };
        options
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE);
    }
    let file = match options.open(&lock_path) {
        Ok(file) => file,
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            options.create_new(false).open(&lock_path)?
        }
        Err(e) => return Err(e),
    };
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(io::Error::other("invalid lock file"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: geteuid is a read-only identity query.
        if metadata.nlink() != 1 || metadata.uid() != unsafe { libc::geteuid() } {
            return Err(io::Error::other("invalid lock ownership or links"));
        }
    }
    match file.try_lock() {
        Ok(()) => (),
        Err(std::fs::TryLockError::WouldBlock) => return Err(io::ErrorKind::WouldBlock.into()),
        Err(std::fs::TryLockError::Error(error)) => return Err(error),
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let current = fs::symlink_metadata(&lock_path)?;
        if current.file_type().is_symlink()
            || current.dev() != metadata.dev()
            || current.ino() != metadata.ino()
            || current.nlink() != 1
        {
            return Err(io::Error::other("lock path changed"));
        }
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    Ok(file)
}

pub fn write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    regular(path)?;
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("missing parent"))?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(bytes)?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?;
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

pub fn backup(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let prefix = format!(
        "{}.plangraph-backup-",
        path.file_name().unwrap().to_string_lossy()
    );
    let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%S%.9fZ");
    let target = path.with_file_name(format!("{prefix}{stamp}"));
    let mut file = private_options().open(target)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    let mut backups = fs::read_dir(path.parent().unwrap())?
        .filter_map(Result::ok)
        .filter(|entry| {
            entry
                .file_name()
                .to_str()
                .is_some_and(|s| s.starts_with(&prefix))
                && entry.file_type().is_ok_and(|t| t.is_file())
        })
        .map(|entry| entry.path())
        .collect::<Vec<_>>();
    backups.sort();
    let excess = backups.len().saturating_sub(3);
    for old in backups.into_iter().take(excess) {
        fs::remove_file(old)?;
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn lock_swap_to_symlink_cannot_open_or_modify_victim() {
        let root = tempfile::tempdir().unwrap();
        let victim = root.path().join("victim");
        fs::write(&victim, b"unchanged").unwrap();
        assert!(lock_with(&root.path().join("target"), |path| {
            std::os::unix::fs::symlink(&victim, path).unwrap();
        })
        .is_err());
        assert_eq!(fs::read(victim).unwrap(), b"unchanged");
    }
}
