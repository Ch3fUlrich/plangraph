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
    let lock_path = path.with_file_name(format!(
        "{}.plangraph-lock",
        path.file_name().unwrap().to_string_lossy()
    ));
    regular(&lock_path)?;
    let file = match private_options().open(&lock_path) {
        Ok(file) => file,
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            OpenOptions::new().write(true).open(&lock_path)?
        }
        Err(e) => return Err(e),
    };
    file.try_lock()
        .map_err(|_| io::Error::other("target busy"))?;
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
