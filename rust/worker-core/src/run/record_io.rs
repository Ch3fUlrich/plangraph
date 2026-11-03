//! Private, same-directory atomic replacement and bounded Windows sharing retries.
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::Path,
    time::Duration,
};

pub fn ensure_private_dir(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)
}

pub fn retry_delay(attempt: usize) -> Duration {
    Duration::from_millis(10 * attempt.saturating_add(1).min(20) as u64)
}

pub(crate) fn retry<T>(
    attempts: usize,
    mut operation: impl FnMut() -> io::Result<T>,
    transient: impl Fn(&io::Error) -> bool,
    mut sleep: impl FnMut(usize),
) -> io::Result<T> {
    assert!(attempts > 0);
    for attempt in 0..attempts {
        match operation() {
            Err(e) if attempt + 1 < attempts && transient(&e) => sleep(attempt),
            result => return result,
        }
    }
    unreachable!()
}
fn transient(error: &io::Error) -> bool {
    #[cfg(windows)]
    {
        matches!(error.raw_os_error(), Some(5 | 32 | 33))
    }
    #[cfg(not(windows))]
    {
        let _ = error;
        false
    }
}

pub fn atomic_write_private(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    ensure_private_dir(parent)?;
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing file name"))?;
    let mut temp_name = std::ffi::OsString::from(".");
    temp_name.push(name);
    temp_name.push(format!("-{:016x}", rand::random::<u64>()));
    let temp = parent.join(temp_name);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temp)?;
    let result = (|| {
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        retry(
            24,
            || fs::rename(&temp, path),
            transient,
            |attempt| std::thread::sleep(retry_delay(attempt)),
        )
    })();
    let _ = fs::remove_file(temp);
    result
}

pub fn append_private(path: &Path, bytes: &[u8]) -> io::Result<()> {
    ensure_private_dir(
        path.parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new(".")),
    )?;
    let mut options = OpenOptions::new();
    options.append(true).create(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    file.write_all(bytes)?;
    file.sync_all()
}
pub fn read_retry(path: &Path) -> io::Result<String> {
    retry(
        if cfg!(windows) { 20 } else { 1 },
        || fs::read_to_string(path),
        transient,
        |_| std::thread::sleep(Duration::from_millis(50)),
    )
}
