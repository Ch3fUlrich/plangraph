//! Admission for a configured pool of mounted worktree filesystems.
use crate::disk_runtime::{disk_space, DiskSpace};
use std::{
    env, io,
    path::{Path, PathBuf},
};

pub const WORKTREE_MOUNTS: &str = "PLANGRAPH_WORKTREE_MOUNTS";
pub const MIN_FREE_BYTES: u64 = 5 << 30;

pub fn select_with<'a>(
    mounts: &[&'a Path],
    min_free_percent: u8,
    sample: impl Fn(&Path) -> io::Result<DiskSpace>,
) -> io::Result<(&'a Path, DiskSpace)> {
    if !(1..100).contains(&min_free_percent) {
        return Err(io::Error::other(
            "disk_low: free percentage must be in 1..=99",
        ));
    }
    let mut best = None;
    let mut failures = Vec::new();
    for &mount in mounts {
        match sample(mount) {
            Ok(space) if space.meets_floor(MIN_FREE_BYTES, min_free_percent) => {
                if best.is_none_or(|(_, previous): (&Path, DiskSpace)| space.free > previous.free) {
                    best = Some((mount, space));
                }
            }
            Ok(space) => failures.push(format!(
                "{}: {} of {} bytes free",
                mount.display(),
                space.free,
                space.total
            )),
            Err(error) => failures.push(format!("{}: {error}", mount.display())),
        }
    }
    best.ok_or_else(|| {
        io::Error::other(format!(
            "disk_low: no work mount meets 5 GiB and {min_free_percent}% free ({})",
            failures.join("; ")
        ))
    })
}

fn worktree_root(mount: &Path) -> PathBuf {
    mount.join("plangraph/worktrees")
}

/// Worktree roots corresponding to every configured mount, including offline mounts.
pub fn configured_roots() -> impl Iterator<Item = PathBuf> {
    env::var_os(WORKTREE_MOUNTS)
        .filter(|value| !value.is_empty())
        .map(|value| {
            env::split_paths(&value)
                .map(|mount| worktree_root(&mount))
                .collect::<Vec<_>>()
        })
        .into_iter()
        .flatten()
}

#[cfg(unix)]
fn mounted_space(mount: &Path) -> io::Result<DiskSpace> {
    use std::os::unix::fs::MetadataExt;
    use std::{process::Command, time::Duration};
    if !mount.is_absolute()
        || mount.parent().is_none()
        || mount.canonicalize()?.as_os_str() != mount.as_os_str()
    {
        return Err(io::Error::other("expected a canonical absolute mount path"));
    }
    crate::host_command::output(
        Command::new("mountpoint").args(["-q", "--"]).arg(mount),
        Duration::from_secs(5),
    )
    .map_err(|_| io::Error::other("work mount unavailable"))?;
    let root = worktree_root(mount);
    let metadata = root.metadata()?;
    if !metadata.is_dir()
        || root.canonicalize()? != root
        || metadata.dev() != mount.metadata()?.dev()
    {
        return Err(io::Error::other(
            "worktree root is outside its work filesystem",
        ));
    }
    disk_space(&root)
}

#[cfg(not(unix))]
fn mounted_space(_: &Path) -> io::Result<DiskSpace> {
    Err(io::Error::other("configured work mounts require Unix"))
}

/// Return the worktree root on the largest eligible disk.
pub fn worktree_disk() -> io::Result<Option<(PathBuf, DiskSpace)>> {
    let Some(configured) = env::var_os(WORKTREE_MOUNTS).filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let mounts: Vec<_> = env::split_paths(&configured).collect();
    let candidates: Vec<_> = mounts.iter().map(PathBuf::as_path).collect();
    let percent = match env::var("PLANGRAPH_DISK_MIN_FREE_PERCENT") {
        Ok(value) => value
            .parse()
            .map_err(|_| io::Error::other("disk_low: invalid free percentage"))?,
        Err(env::VarError::NotPresent) => 10,
        Err(error) => return Err(io::Error::other(error)),
    };
    let (mount, space) = select_with(&candidates, percent, mounted_space)?;
    Ok(Some((worktree_root(mount), space)))
}
