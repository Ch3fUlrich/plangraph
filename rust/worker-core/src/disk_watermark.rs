//! Sample configured build, worktree and scratch directories once per disk crossing.
use crate::disk_hygiene::Report;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, HashSet},
    fs, io,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};

#[derive(Debug, Deserialize, Serialize)]
struct Directory {
    path: PathBuf,
    bytes: u64,
}

#[derive(Debug, Deserialize, Serialize)]
struct Alert {
    free: u64,
    total: u64,
    directories: Vec<Directory>,
}

fn observe(
    active: &mut BTreeMap<u64, Alert>,
    roots: &BTreeMap<PathBuf, u64>,
    sample: impl Fn(&Path) -> io::Result<(u64, u64)>,
    measure: impl Fn(&Path, u64) -> u64,
) -> Vec<u64> {
    let mut devices = BTreeMap::<_, Vec<_>>::new();
    for (path, device) in roots {
        devices.entry(*device).or_default().push(path);
    }
    let mut crossed = vec![];
    for (device, paths) in devices {
        let Some((free, total)) = paths.iter().find_map(|p| sample(p).ok()) else {
            continue;
        };
        if total == 0 {
            continue;
        }
        if u128::from(free) * 100 > u128::from(total) * 15 {
            active.remove(&device);
            continue;
        }
        if let std::collections::btree_map::Entry::Vacant(entry) = active.entry(device) {
            let mut directories: Vec<_> = paths
                .into_iter()
                .map(|path| Directory {
                    path: path.clone(),
                    bytes: measure(path, device),
                })
                .collect();
            directories.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
            directories.truncate(3);
            entry.insert(Alert {
                free,
                total,
                directories,
            });
            crossed.push(device);
        }
    }
    crossed
}

fn allocated(path: &Path, device: u64, roots: &BTreeMap<PathBuf, u64>) -> u64 {
    let mut seen = HashSet::new();
    let mut pending = vec![path.to_path_buf()];
    let mut bytes = 0u64;
    while let Some(candidate) = pending.pop() {
        let Ok(meta) = fs::symlink_metadata(&candidate) else {
            continue;
        };
        if meta.dev() != device
            || (candidate != path && roots.contains_key(&candidate))
            || ((meta.is_dir() || meta.nlink() > 1) && !seen.insert(meta.ino()))
        {
            continue;
        }
        bytes = bytes.saturating_add(meta.blocks().saturating_mul(512));
        if meta.is_dir() {
            pending.extend(
                fs::read_dir(candidate)
                    .into_iter()
                    .flatten()
                    .flatten()
                    .map(|entry| entry.path()),
            );
        }
    }
    bytes
}

/// Save active watermark events and return only the newly crossed devices.
fn record_crossings<'a>(
    state: &Path,
    paths: impl IntoIterator<Item = &'a Path>,
) -> io::Result<Vec<String>> {
    let mut roots = BTreeMap::new();
    let mut seen = HashSet::new();
    for path in paths {
        let Ok(path) = path.canonicalize() else {
            continue;
        };
        let Ok(meta) = path.metadata() else { continue };
        if meta.is_dir() && seen.insert((meta.dev(), meta.ino())) {
            roots.insert(path, meta.dev());
        }
    }
    crate::run::record_io::ensure_private_dir(state)?;
    let file = state.join("disk-watermarks.json");
    let Some(_lease) = crate::cargo_cache::try_exclusive_lease(&file)? else {
        return Ok(vec![]);
    };
    let mut active: BTreeMap<u64, Alert> = match fs::read(&file) {
        Ok(bytes) => serde_json::from_slice(&bytes)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => BTreeMap::new(),
        Err(error) => return Err(error),
    };
    let previous: Vec<_> = active.keys().copied().collect();
    let crossed = observe(
        &mut active,
        &roots,
        |path| crate::disk_runtime::disk_space(path).map(|space| (space.free, space.total)),
        |path, device| allocated(path, device, &roots),
    );
    if !active.keys().copied().eq(previous) {
        crate::run::record_io::atomic_write_private(&file, &serde_json::to_vec(&active)?)?;
    }
    crossed
        .into_iter()
        .map(|device| {
            Ok(format!(
                "disk watermark 85% device={device} {}",
                serde_json::to_string(&active[&device])?
            ))
        })
        .collect()
}

/// Sample configured hygiene roots and add new crossings to the report.
pub fn record_configured(
    state: &Path,
    worktrees: impl IntoIterator<Item = PathBuf>,
    shared: &[PathBuf],
    scratch: &[PathBuf],
    report: &mut Report,
) {
    let builds = shared
        .iter()
        .flat_map(|root| crate::cargo_pool::existing_pool_slots(root));
    let roots: Vec<_> = worktrees
        .into_iter()
        .chain(builds)
        .chain(scratch.iter().cloned())
        .collect();
    match record_crossings(state, roots.iter().map(PathBuf::as_path)) {
        Ok(events) => {
            report.alarm |= !events.is_empty();
            for event in events {
                eprintln!("ALERT {event}");
                report.actions.push(event);
            }
        }
        Err(error) => report
            .skipped
            .push(format!("disk watermark unavailable: {error}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    #[test]
    fn watermark_ranks_three_same_filesystem_roots_and_rearms_below_85() {
        let roots = BTreeMap::from([
            (PathBuf::from("/fixture/worktrees"), 1),
            (PathBuf::from("/fixture/build"), 1),
            (PathBuf::from("/fixture/scratch"), 1),
            (PathBuf::from("/fixture/pool"), 1),
            (PathBuf::from("/offline/build"), 2),
        ]);
        let free = Cell::new(16);
        let mut active = BTreeMap::new();
        let mut pulse = || {
            observe(
                &mut active,
                &roots,
                |path| {
                    Ok((
                        if path.starts_with("/fixture") {
                            free.get()
                        } else {
                            20
                        },
                        100,
                    ))
                },
                |path, device| {
                    assert_eq!(device, 1);
                    match path.file_name().unwrap().to_str().unwrap() {
                        "build" => 40,
                        "scratch" => 30,
                        "worktrees" => 20,
                        "pool" => 10,
                        _ => panic!("root on a separate filesystem"),
                    }
                },
            )
        };
        assert!(pulse().is_empty());
        free.set(15);
        assert_eq!(pulse(), [1]);
        assert!(pulse().is_empty());
        free.set(16);
        assert!(pulse().is_empty());
        free.set(12);
        assert_eq!(pulse(), [1]);
        let alert = &active[&1];
        assert_eq!((alert.free, alert.total), (12, 100));
        assert_eq!(
            alert
                .directories
                .iter()
                .map(|row| (row.path.as_path(), row.bytes))
                .collect::<Vec<_>>(),
            [
                (Path::new("/fixture/build"), 40),
                (Path::new("/fixture/scratch"), 30),
                (Path::new("/fixture/worktrees"), 20),
            ]
        );
    }
}
