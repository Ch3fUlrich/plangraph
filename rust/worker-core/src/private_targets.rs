//! Reclaim abandoned Cargo targets without using directory names as evidence.
use crate::disk_hygiene::{scan, Guards, Report};
use std::{
    fs, io,
    path::Path,
    time::{Duration, SystemTime},
};

pub(crate) fn cargo_target(path: &Path) -> bool {
    fs::symlink_metadata(path.join(".rustc_info.json")).is_ok_and(|entry| entry.is_file())
        && ["debug", "release"].iter().any(|profile| {
            fs::symlink_metadata(path.join(profile)).is_ok_and(|entry| entry.is_dir())
        })
        && fs::symlink_metadata(path.join("Cargo.toml"))
            .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
}

#[cfg(target_os = "linux")]
pub(crate) use process_paths::{process_cargo_path, resolve_deleted_path};

#[cfg(target_os = "linux")]
mod process_paths {
    use super::*;
    use crate::disk_hygiene::{file_identity, invalid_inventory};
    use std::{
        ffi::CString,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::{ffi::OsStrExt, fs::OpenOptionsExt},
        },
        path::PathBuf,
    };

    // Walk back only over ordinary missing names. An unresolved `..` is ambiguous.
    fn resolve_suffix(
        path: &Path,
        mut resolve: impl FnMut(&Path) -> io::Result<PathBuf>,
    ) -> io::Result<PathBuf> {
        let mut prefix = path;
        loop {
            match resolve(prefix) {
                Ok(mut base) => {
                    let suffix = path.strip_prefix(prefix).unwrap();
                    if !suffix.as_os_str().is_empty() {
                        base.push(suffix);
                    }
                    return Ok(base);
                }
                Err(error)
                    if error.kind() == io::ErrorKind::NotFound && prefix.file_name().is_some() =>
                {
                    prefix = prefix.parent().unwrap()
                }
                Err(error) => return Err(error),
            }
        }
    }

    pub(crate) fn resolve_deleted_path(
        path: &Path,
        identity: Option<(u64, u64)>,
    ) -> io::Result<PathBuf> {
        resolve_suffix(path, |prefix| {
            let host = fs::canonicalize(prefix)?;
            if let Some((device, _)) = identity {
                if file_identity(&fs::metadata(&host)?).0 != device {
                    return Err(invalid_inventory());
                }
            }
            Ok(host)
        })
    }

    fn open_rooted(root: &fs::File, path: &Path, flags: i32) -> io::Result<fs::File> {
        let name = CString::new(path.as_os_str().as_bytes())?;
        // SAFETY: zero is a valid open_how, the zero-extension of its flags.
        let mut how: libc::open_how = unsafe { std::mem::zeroed() };
        how.flags = (libc::O_PATH | libc::O_CLOEXEC | flags) as u64;
        how.resolve = libc::RESOLVE_IN_ROOT | libc::RESOLVE_NO_MAGICLINKS;
        // SAFETY: both pointers remain valid for the syscall; a returned fd is owned here.
        let fd = unsafe {
            libc::syscall(
                libc::SYS_openat2,
                root.as_raw_fd(),
                name.as_ptr(),
                &how,
                std::mem::size_of_val(&how),
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { fs::File::from_raw_fd(fd as i32) })
    }

    pub(crate) fn process_cargo_path(pid: &Path, value: &Path) -> io::Result<PathBuf> {
        let root_link = pid.join("root");
        let root = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_PATH | libc::O_DIRECTORY)
            .open(&root_link)?;
        let value = if value.is_absolute() {
            value.to_path_buf()
        } else {
            let cwd = pid.join("cwd");
            let name = fs::read_link(&cwd)?;
            let name = name
                .strip_prefix(fs::read_link(&root_link)?)
                .map_err(|_| invalid_inventory())?;
            let name = Path::new("/").join(name);
            if file_identity(&open_rooted(&root, &name, 0)?.metadata()?)
                != file_identity(&fs::metadata(cwd)?)
            {
                return Err(invalid_inventory());
            }
            name.join(value)
        };
        resolve_suffix(&value, |prefix| {
            let file = match open_rooted(&root, prefix, 0) {
                Err(error)
                    if error.kind() == io::ErrorKind::NotFound
                        && open_rooted(&root, prefix, libc::O_NOFOLLOW).is_ok() =>
                {
                    return Err(invalid_inventory())
                }
                result => result?,
            };
            let name = fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd()))?;
            if !name.is_absolute()
                || file_identity(&file.metadata()?)
                    != file_identity(&fs::metadata(&name).map_err(|_| invalid_inventory())?)
            {
                return Err(invalid_inventory());
            }
            Ok(name)
        })
    }
}

pub(crate) fn owned(path: &Path) -> io::Result<bool> {
    #[cfg(unix)]
    {
        // SAFETY: geteuid takes no arguments and has no preconditions.
        owned_by(path, unsafe { libc::geteuid() })
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(false)
    }
}

#[cfg(unix)]
pub(crate) fn owned_by(path: &Path, uid: u32) -> io::Result<bool> {
    use std::os::unix::fs::MetadataExt;
    Ok(fs::metadata(path)?.uid() == uid)
}

/// `Ok(None)` when the entry vanished meanwhile; any other error stays an error.
fn skip_vanished<T>(result: io::Result<T>) -> io::Result<Option<T>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

/// Walk only two levels in the host temporary directory. Worktree targets are
/// supplied separately from Git's authoritative worktree inventory.
pub fn sweep(
    temporary: Option<&Path>,
    worktrees: &[std::path::PathBuf],
    guards: &Guards,
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    sweep_using(
        temporary,
        worktrees,
        guards,
        SystemTime::now(),
        apply,
        report,
        crate::disk_runtime::processes,
    )
}

fn sweep_using(
    temporary: Option<&Path>,
    worktrees: &[std::path::PathBuf],
    guards: &Guards,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    mut processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    let mut targets = vec![];
    if let Some(temporary) = temporary {
        for entry in fs::read_dir(temporary)? {
            let entry = entry?;
            // Another process may remove an entry between listing and inspection.
            let Some(is_dir) = skip_vanished(entry.file_type().map(|t| t.is_dir()))? else {
                continue;
            };
            if !is_dir || !skip_vanished(owned(&entry.path()))?.unwrap_or(false) {
                continue;
            }
            let path = entry.path();
            #[cfg(target_os = "linux")]
            if crate::affected_scratch::is_run(&path) {
                skip_vanished(crate::affected_scratch::reclaim(
                    &path, guards, now, apply, report,
                ))?;
                continue;
            }
            if cargo_target(&path) {
                targets.push((path, None));
            } else if let Some(children) = skip_vanished(fs::read_dir(&path))? {
                for child in children {
                    let Some(child) = skip_vanished(child)? else {
                        continue;
                    };
                    if skip_vanished(child.file_type().map(|t| t.is_dir()))? == Some(true)
                        && cargo_target(&child.path())
                    {
                        targets.push((child.path(), None));
                    }
                }
            }
        }
    }
    for tree in worktrees {
        if crate::disk_hygiene::git(tree, &["ls-files", "--", "rust/target"])?.is_empty()
            && crate::disk_hygiene::git(
                tree,
                &[
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "--directory",
                    "--",
                    "rust/target/",
                ],
            )?
            .trim()
                == "rust/target/"
        {
            let last_commit = crate::disk_hygiene::git(tree, &["log", "-1", "--format=%ct"])
                .ok()
                .and_then(|value| value.trim().parse::<u64>().ok())
                .and_then(|seconds| {
                    SystemTime::UNIX_EPOCH.checked_add(Duration::from_secs(seconds))
                });
            targets.push((tree.join("rust/target"), last_commit));
        }
    }
    for (target, last_commit) in targets {
        reclaim(
            &target,
            guards,
            last_commit,
            now,
            apply,
            report,
            &mut processes,
        )?;
    }
    Ok(())
}

fn reclaim(
    target: &Path,
    guards: &Guards,
    last_commit: Option<SystemTime>,
    now: SystemTime,
    apply: bool,
    report: &mut Report,
    mut processes: impl FnMut() -> io::Result<(Vec<std::path::PathBuf>, bool)>,
) -> io::Result<()> {
    if !target.exists() || target.is_symlink() || !cargo_target(target) {
        return Ok(());
    }
    if !owned(target)? {
        return Ok(());
    }
    let canonical = target.canonicalize()?;
    // Reject symlinked parents (notably a worktree's rust directory).
    if canonical != target || guards.protects(&canonical) {
        return Ok(());
    }
    let Some(_lease) = crate::cargo_cache::try_exclusive_lease(target)? else {
        return Ok(());
    };
    let (bytes, newest) = scan(target)?;
    if last_commit.is_none_or(|commit| newest >= commit)
        && now.duration_since(newest).unwrap_or_default() <= Duration::from_secs(1800)
    {
        return Ok(());
    }
    let (cwds, building) = processes()?;
    if building || cwds.iter().any(|p| p.starts_with(&canonical)) {
        return Ok(());
    }
    if apply {
        fs::remove_dir_all(target)?;
        report.freed_bytes = report.freed_bytes.saturating_add(bytes);
    }
    report.actions.push(format!(
        "{} {} bytes={bytes}",
        if apply { "removed" } else { "would-remove" },
        target.display()
    ));
    Ok(())
}

#[cfg(all(test, unix))]
#[path = "private_targets_tests.rs"]
mod tests;
