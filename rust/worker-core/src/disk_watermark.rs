//! Sample configured build, worktree and scratch directories at each disk crossing above 85% used.
use crate::disk_hygiene::Report;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, HashSet},
    fs, io,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};

/// The alert names this many of the largest roots (rule 9.23).
const TOP_CONSUMERS: usize = 5;

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
    /// When the directories were measured. A row written before this field existed has none: its
    /// freshness is unknown, and the next successful hot measurement replaces it.
    #[serde(default)]
    measured_at: Option<u64>,
}

/// One pass over the filesystems of `roots` at time `now`. A filesystem above 85% used is measured
/// again on every pass, so its row never outlives the latest hot sample; one at or below 85% is
/// forgotten. Returns the devices that crossed on this pass.
fn observe(
    active: &mut BTreeMap<u64, Alert>,
    roots: &BTreeMap<PathBuf, u64>,
    now: u64,
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
        // Used strictly above 85% crosses; exactly 85% is recovery, as in the watchdog.
        if u128::from(free) * 100 >= u128::from(total) * 15 {
            active.remove(&device);
            continue;
        }
        let mut directories: Vec<_> = paths
            .into_iter()
            .map(|path| Directory {
                path: path.clone(),
                bytes: measure(path, device),
            })
            .collect();
        directories.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
        directories.truncate(TOP_CONSUMERS);
        let alert = Alert {
            free,
            total,
            directories,
            measured_at: Some(now),
        };
        if active.insert(device, alert).is_none() {
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
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    record_sampled(state, paths, now, |path| {
        crate::disk_runtime::disk_space(path).map(|space| (space.free, space.total))
    })
}

fn record_sampled<'a>(
    state: &Path,
    paths: impl IntoIterator<Item = &'a Path>,
    now: u64,
    sample: impl Fn(&Path) -> io::Result<(u64, u64)>,
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
    let stored = match fs::read(&file) {
        Ok(bytes) => Some(bytes),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    let mut active: BTreeMap<u64, Alert> = match &stored {
        Some(bytes) => serde_json::from_slice(bytes)?,
        None => BTreeMap::new(),
    };
    let crossed = observe(&mut active, &roots, now, sample, |path, device| {
        allocated(path, device, &roots)
    });
    let updated = serde_json::to_vec(&active)?;
    if stored
        .as_deref()
        .map_or(!active.is_empty(), |old| old != updated)
    {
        crate::run::record_io::atomic_write_private(&file, &updated)?;
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

    fn roots() -> BTreeMap<PathBuf, u64> {
        BTreeMap::from([
            (PathBuf::from("/fixture/worktrees"), 1),
            (PathBuf::from("/fixture/build"), 1),
            (PathBuf::from("/fixture/scratch"), 1),
            (PathBuf::from("/fixture/pool"), 1),
            (PathBuf::from("/fixture/tmp"), 1),
            (PathBuf::from("/fixture/cache"), 1),
            (PathBuf::from("/offline/build"), 2),
        ])
    }

    fn pulse(
        active: &mut BTreeMap<u64, Alert>,
        free: &Cell<u64>,
        now: u64,
        measure: impl Fn(&Path, u64) -> u64,
    ) -> Vec<u64> {
        observe(
            active,
            &roots(),
            now,
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
            measure,
        )
    }

    fn sizes(path: &Path, device: u64) -> u64 {
        assert_eq!(device, 1);
        match path.file_name().unwrap().to_str().unwrap() {
            "tmp" => 50,
            "build" => 40,
            "scratch" => 30,
            "worktrees" => 20,
            "pool" => 10,
            "cache" => 5,
            _ => panic!("root on a separate filesystem"),
        }
    }

    #[test]
    fn watermark_ranks_five_same_filesystem_roots_at_86_percent() {
        let free = Cell::new(16);
        let mut active = BTreeMap::new();
        assert!(pulse(&mut active, &free, 1, sizes).is_empty());
        free.set(14);
        assert_eq!(pulse(&mut active, &free, 2, sizes), [1]);
        let alert = &active[&1];
        assert_eq!(
            (alert.free, alert.total, alert.measured_at),
            (14, 100, Some(2))
        );
        assert_eq!(
            alert
                .directories
                .iter()
                .map(|row| (row.path.as_path(), row.bytes))
                .collect::<Vec<_>>(),
            [
                (Path::new("/fixture/tmp"), 50),
                (Path::new("/fixture/build"), 40),
                (Path::new("/fixture/scratch"), 30),
                (Path::new("/fixture/worktrees"), 20),
                (Path::new("/fixture/pool"), 10),
            ]
        );
    }

    #[test]
    fn watermark_rearms_at_exactly_85_percent() {
        let free = Cell::new(14);
        let mut active = BTreeMap::new();
        assert_eq!(pulse(&mut active, &free, 1, sizes), [1]);
        free.set(15);
        assert!(pulse(&mut active, &free, 2, sizes).is_empty());
        assert!(active.is_empty(), "85% used is recovery");
        free.set(14);
        assert_eq!(pulse(&mut active, &free, 3, sizes), [1]);
    }

    #[test]
    fn a_hot_filesystem_is_measured_again_on_every_pass() {
        let free = Cell::new(10);
        let mut active = BTreeMap::new();
        assert_eq!(pulse(&mut active, &free, 1, sizes), [1]);
        assert!(pulse(&mut active, &free, 2, |_, _| 7).is_empty());
        let alert = &active[&1];
        assert_eq!(alert.measured_at, Some(2));
        assert!(alert.directories.iter().all(|row| row.bytes == 7));
    }

    #[test]
    fn rows_without_a_timestamp_still_load_and_the_next_hot_pass_replaces_them() {
        let mut active: BTreeMap<u64, Alert> = serde_json::from_str(
            r#"{"1":{"free":10,"total":100,"directories":[{"path":"/old","bytes":1}]}}"#,
        )
        .unwrap();
        assert_eq!(active[&1].measured_at, None);
        let free = Cell::new(10);
        assert!(pulse(&mut active, &free, 9, sizes).is_empty());
        assert_eq!(active[&1].measured_at, Some(9));
        assert_ne!(active[&1].directories[0].path, Path::new("/old"));
    }

    #[test]
    fn record_crossings_refreshes_the_file_while_the_device_key_stays_the_same() {
        let state = tempfile::tempdir().unwrap();
        let root = tempfile::tempdir().unwrap();
        let device = root.path().metadata().unwrap().dev().to_string();
        let hot = |_: &Path| Ok((10, 100));
        let paths = [root.path()];
        let events = record_sampled(state.path(), paths, 100, hot).unwrap();
        assert_eq!(events.len(), 1);
        let file = state.path().join("disk-watermarks.json");
        let read =
            || -> serde_json::Value { serde_json::from_slice(&fs::read(&file).unwrap()).unwrap() };
        assert_eq!(read()[&device]["measured_at"], 100);
        assert!(record_sampled(state.path(), paths, 200, hot)
            .unwrap()
            .is_empty());
        assert_eq!(read()[&device]["measured_at"], 200);
        let cool = |_: &Path| Ok((15, 100));
        assert!(record_sampled(state.path(), paths, 300, cool)
            .unwrap()
            .is_empty());
        assert!(read().as_object().unwrap().is_empty());
    }
}
