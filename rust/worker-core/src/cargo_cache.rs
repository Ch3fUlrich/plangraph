//! Shared cache paths and the host_ready.py cargo_lease contract.
use std::{
    env,
    ffi::OsStr,
    fs::{File, OpenOptions},
    io,
    path::{Path, PathBuf},
};

pub fn shared_cache_roots() -> io::Result<Vec<PathBuf>> {
    let home =
        env::var_os("HOME").ok_or_else(|| io::Error::other("HOME required for shared caches"))?;
    cache_roots(
        env::var_os("PLANGRAPH_CARGO_TARGET_DIR").as_deref(),
        Path::new(&home),
        &env::current_dir()?,
    )
}

/// Check the actual child setting against the host's configured writer target.
pub fn require_writer_target(checkout: &Path, value: Option<&OsStr>) -> io::Result<PathBuf> {
    let refuse = |reason: &str| io::Error::other(format!("CARGO_TARGET_DIR: {reason}"));
    let value = value
        .filter(|value| !value.is_empty())
        .ok_or_else(|| refuse("writer requires an explicit shared target"))?;
    let actual = crate::workspace_launch::resolved(Path::new(value))
        .map_err(|_| refuse("expected an absolute, resolvable shared target"))?;
    let configured = crate::workspace_launch::resolved(&shared_cache_roots()?.remove(0))
        .map_err(|_| refuse("configured shared target cannot be resolved"))?;
    let cwd = checkout.canonicalize()?;
    let checkout = cwd
        .ancestors()
        .find(|path| path.join(".git").exists())
        .unwrap_or(&cwd);
    if actual.starts_with(checkout) || checkout.starts_with(&actual) {
        return Err(refuse("the writer target overlaps the checkout"));
    }
    if actual != configured || (actual.exists() && !actual.is_dir()) {
        return Err(refuse(
            "writer target must match the configured shared directory",
        ));
    }
    Ok(actual)
}

/// The shared target of a host: `configured` (`PLANGRAPH_CARGO_TARGET_DIR`, `~` and relative paths
/// expanded against `home` and `cwd`) or the default. The one resolver every launcher uses.
pub fn shared_target_from(
    configured: Option<&OsStr>,
    home: &Path,
    cwd: &Path,
) -> io::Result<PathBuf> {
    Ok(cache_roots(configured, home, cwd)?.remove(0))
}

fn cache_roots(configured: Option<&OsStr>, home: &Path, cwd: &Path) -> io::Result<Vec<PathBuf>> {
    let configured = Path::new(configured.unwrap_or(OsStr::new("~/.cache/plangraph/cargo-target")));
    let mut target = configured.to_path_buf();
    if let Some(first) = configured.components().next() {
        let first = first.as_os_str();
        if first == "~" {
            target = home.join(configured.strip_prefix(first).unwrap());
        } else if let Some(user) = first.to_str().and_then(|s| s.strip_prefix('~')) {
            target = user_home(user)?.join(configured.strip_prefix(first).unwrap());
        }
    }
    if !target.is_absolute() {
        target = cwd.join(target);
    }
    // Path.absolute() in Python preserves '..' and does not resolve symlinks.
    let plan_order = home.join(".cache/pg-target-po");
    let mut roots = vec![target];
    if roots[0] != plan_order {
        roots.push(plan_order);
    }
    Ok(roots)
}

#[cfg(unix)]
fn user_home(user: &str) -> io::Result<PathBuf> {
    use std::os::unix::ffi::OsStrExt;
    let name = std::ffi::CString::new(user)?;
    let mut buffer = vec![0u8; 16384];
    loop {
        let mut pwd = std::mem::MaybeUninit::<libc::passwd>::uninit();
        let mut found = std::ptr::null_mut();
        // SAFETY: all pointers reference writable storage for the supplied sizes;
        // the returned fields remain valid while buffer is alive.
        let result = unsafe {
            libc::getpwnam_r(
                name.as_ptr(),
                pwd.as_mut_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut found,
            )
        };
        if result == libc::ERANGE {
            buffer.resize(buffer.len() * 2, 0);
            continue;
        }
        if result != 0 {
            return Err(io::Error::from_raw_os_error(result));
        }
        if found.is_null() {
            return Err(io::Error::other("cannot resolve home directory"));
        }
        // SAFETY: a successful lookup initialized pwd and its NUL-terminated directory.
        let directory = unsafe { std::ffi::CStr::from_ptr(pwd.assume_init().pw_dir) };
        return Ok(PathBuf::from(OsStr::from_bytes(directory.to_bytes())));
    }
}
#[cfg(not(unix))]
fn user_home(_: &str) -> io::Result<PathBuf> {
    Err(io::Error::other("named home expansion requires Unix"))
}

#[cfg(unix)]
#[derive(Debug)]
pub struct CacheLease(File);
#[cfg(target_os = "linux")]
impl CacheLease {
    /// Lock a validated lease file until the caller has finished reclaiming.
    pub(crate) fn try_exclusive_file(file: File) -> io::Result<Option<Self>> {
        try_lock_file(file, libc::LOCK_EX)
    }

    pub(crate) fn file(&self) -> &File {
        &self.0
    }
}

#[cfg(unix)]
impl Drop for CacheLease {
    fn drop(&mut self) {
        use std::os::fd::AsRawFd;
        // Closing alone leaves the flock held if a concurrent fork inherited
        // this open file description before exec. End the reclaim lease now.
        // SAFETY: self owns the descriptor and reclaim has finished.
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}
#[cfg(unix)]
pub type ExclusiveLease = CacheLease;
#[cfg(not(unix))]
pub type ExclusiveLease = File;

/// Nonblocking exclusive flock on <target>.plangraph-lock, also used by Python.
/// Keep the returned guard alive for the entire reclaim; never unlink the lock.
#[cfg(unix)]
pub fn try_exclusive_lease(target: &Path) -> io::Result<Option<ExclusiveLease>> {
    try_lease(target, libc::LOCK_EX)
}

/// Guard a writer from start through child exit using the same lease as preflight.
#[cfg(target_os = "linux")]
pub fn writer_lease(target: &Path) -> io::Result<CacheLease> {
    let parent = target
        .parent()
        .ok_or_else(|| io::Error::other("cache root needs a parent"))?;
    std::fs::create_dir_all(parent)?;
    try_lease(target, libc::LOCK_SH)?.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::WouldBlock,
            "CARGO_TARGET_DIR: shared cache is being reclaimed",
        )
    })
}

#[cfg(unix)]
fn try_lease(target: &Path, mode: libc::c_int) -> io::Result<Option<CacheLease>> {
    let mut name = target
        .components()
        .next_back()
        .map(|c| c.as_os_str())
        .filter(|name| *name != OsStr::new("/"))
        .ok_or_else(|| io::Error::other("cache root needs a name"))?
        .to_os_string();
    name.push(".plangraph-lock");
    let lease = OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(
            target
                .parent()
                .ok_or_else(|| io::Error::other("cache root needs a parent"))?
                .join(name),
        )?;
    try_lock_file(lease, mode)
}

#[cfg(unix)]
fn try_lock_file(lease: File, mode: libc::c_int) -> io::Result<Option<CacheLease>> {
    use std::os::fd::AsRawFd;
    // SAFETY: lease owns a valid descriptor; flock does not retain pointers.
    if unsafe { libc::flock(lease.as_raw_fd(), mode | libc::LOCK_NB) } == 0 {
        return Ok(Some(CacheLease(lease)));
    }
    let error = io::Error::last_os_error();
    if error.kind() == io::ErrorKind::WouldBlock {
        Ok(None)
    } else {
        Err(error)
    }
}
#[cfg(not(unix))]
pub fn try_exclusive_lease(_: &Path) -> io::Result<Option<ExclusiveLease>> {
    Err(io::Error::other("disk hygiene requires a Unix host"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    #[cfg(target_os = "linux")]
    fn writer_lease_allows_another_writer_and_protects_from_cleanup() {
        let parent = tempfile::tempdir().unwrap();
        let cache = parent.path().join("target");
        let first = writer_lease(&cache).unwrap();
        let second = writer_lease(&cache).unwrap();
        assert!(try_exclusive_lease(&cache).unwrap().is_none());
        drop(first);
        assert!(try_exclusive_lease(&cache).unwrap().is_none());
        drop(second);
        let cleanup = try_exclusive_lease(&cache).unwrap().unwrap();
        assert_eq!(
            writer_lease(&cache).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        drop(cleanup);
        assert!(writer_lease(&cache).is_ok());
    }
    #[test]
    #[cfg(unix)]
    fn reclaim_releases_lease_even_with_an_inherited_descriptor() {
        let parent = tempfile::tempdir().unwrap();
        let cache = parent.path().join("target");
        let lease = try_exclusive_lease(&cache).unwrap().unwrap();
        // dup and fork share the same open file description. Model a concurrent
        // spawn retaining it before exec closes its CLOEXEC descriptors.
        let inherited = lease.0.try_clone().unwrap();
        assert!(try_exclusive_lease(&cache).unwrap().is_none());
        drop(lease);
        assert!(try_exclusive_lease(&cache).unwrap().is_some());
        drop(inherited);
    }
    #[test]
    fn roots_match_host_ready_defaults_overrides_and_expanduser() {
        let home = Path::new("/srv/fixture-home");
        let cwd = Path::new("/work/tree");
        for (value, expected) in [
            (None, "/srv/fixture-home/.cache/plangraph/cargo-target"),
            (Some("~/shared"), "/srv/fixture-home/shared"),
            (Some("/mnt/cache"), "/mnt/cache"),
            (Some("relative/cache"), "/work/tree/relative/cache"),
            (Some(""), "/work/tree/"),
        ] {
            let roots = cache_roots(value.map(OsStr::new), home, cwd).unwrap();
            assert_eq!(
                roots,
                vec![PathBuf::from(expected), home.join(".cache/pg-target-po")]
            );
        }
        assert_eq!(
            cache_roots(Some(OsStr::new("~/.cache/pg-target-po")), home, cwd)
                .unwrap()
                .len(),
            1
        );
        #[cfg(unix)]
        assert_eq!(
            cache_roots(Some(OsStr::new("~root/cache")), home, cwd).unwrap()[0],
            user_home("root").unwrap().join("cache")
        );
    }
}
