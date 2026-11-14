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

/// Nonblocking exclusive flock on <target>.plangraph-lock, also used by Python.
/// Keep the returned file alive for the entire reclaim; never unlink the lock.
#[cfg(unix)]
pub(crate) fn try_exclusive_lease(target: &Path) -> io::Result<Option<File>> {
    use std::os::fd::AsRawFd;
    let mut name = target
        .file_name()
        .ok_or_else(|| io::Error::other("cache root needs a name"))?
        .to_os_string();
    name.push(".plangraph-lock");
    let lease = OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(target.with_file_name(name))?;
    // SAFETY: lease owns a valid descriptor; flock does not retain pointers.
    if unsafe { libc::flock(lease.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(Some(lease));
    }
    let error = io::Error::last_os_error();
    if error.kind() == io::ErrorKind::WouldBlock {
        Ok(None)
    } else {
        Err(error)
    }
}
#[cfg(not(unix))]
pub(crate) fn try_exclusive_lease(_: &Path) -> io::Result<Option<File>> {
    Err(io::Error::other("disk hygiene requires a Unix host"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roots_match_host_ready_defaults_overrides_and_expanduser() {
        let home = Path::new("/home/fixture");
        let cwd = Path::new("/work/tree");
        for (value, expected) in [
            (None, "/home/fixture/.cache/plangraph/cargo-target"),
            (Some("~/shared"), "/home/fixture/shared"),
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
