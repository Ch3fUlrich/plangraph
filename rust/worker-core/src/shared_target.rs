//! Age-ordered shared Cargo target eviction, retaining newest compile/run variants per crate.
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

fn directories(path: &Path) -> io::Result<Vec<PathBuf>> {
    let entries = match fs::read_dir(path) {
        Ok(entries) => entries,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e),
    };
    let mut paths = vec![];
    for entry in entries {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            paths.push(entry.path());
        }
    }
    Ok(paths)
}

/// First bytes of the `CACHEDIR.TAG` cargo writes at the root of every target directory.
pub(crate) const CACHEDIR_SIGNATURE: &[u8] = b"Signature: 8a477f597d28d172789f06886806bc55";

#[cfg(target_os = "linux")]
fn cargo_output(root: &Path) -> bool {
    let marker = |name| {
        let path = root.join(name);
        let meta = fs::symlink_metadata(&path).ok()?;
        (meta.is_file() && meta.len() <= 1 << 20)
            .then(|| fs::read(path).ok())
            .flatten()
    };
    !root.join(".git").exists()
        && (marker("CACHEDIR.TAG").is_some_and(|tag| tag.starts_with(CACHEDIR_SIGNATURE))
            || marker(".rustc_info.json").is_some_and(|info| {
                serde_json::from_slice::<serde_json::Value>(&info)
                    .is_ok_and(|value| value.get("rustc_fingerprint").is_some())
            }))
}

/// Recognize debug output by Cargo's target and profile markers before whole-profile eviction.
/// A mounted profile is listed too: the removal guard refuses it and reports why.
#[cfg(target_os = "linux")]
pub(crate) fn debug_profiles(root: &Path) -> io::Result<Vec<(PathBuf, u64)>> {
    if !cargo_output(root) {
        return Ok(vec![]);
    }
    let mut candidates = vec![];
    let triples = directories(root)?.into_iter().filter(|path| {
        path.file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.split('-').count() >= 3)
    });
    for parent in std::iter::once(root.to_path_buf()).chain(triples) {
        let profile = parent.join("debug");
        if !fs::symlink_metadata(&profile).is_ok_and(|meta| meta.is_dir())
            || ![".cargo-lock", ".cargo-build-lock"].iter().any(|name| {
                fs::symlink_metadata(profile.join(name)).is_ok_and(|meta| meta.is_file())
            })
            || !crate::private_targets::owned(&profile)?
        {
            continue;
        }
        let (bytes, newest) = crate::disk_hygiene::scan(&profile)?;
        candidates.push((newest, profile, bytes));
    }
    candidates.sort();
    Ok(candidates
        .into_iter()
        .map(|(_, path, bytes)| (path, bytes))
        .collect())
}

#[cfg(not(target_os = "linux"))]
pub(crate) fn debug_profiles(_: &Path) -> io::Result<Vec<(PathBuf, u64)>> {
    Ok(vec![])
}

fn hash(value: &str) -> bool {
    value.len() == 16 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn build_units(parent: &Path) -> io::Result<Vec<(String, PathBuf, bool)>> {
    let mut units = vec![];
    for path in directories(parent)? {
        let Some(name) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if let Some((name, _)) = name.rsplit_once('-').filter(|(_, suffix)| hash(suffix)) {
            let compile = path.join("build-script-build").is_file()
                || path.join("build-script-build.exe").is_file();
            let run = path.join("output").is_file() || path.join("invoked.timestamp").is_file();
            if compile || run {
                units.push((name.to_owned(), path.clone(), compile));
            }
        }
        // Cargo 1.100 also stores units as build/<crate>/<hash>/, alongside
        // ordinary library artifacts. Only recognize build-script units.
        for unit in directories(&path)? {
            if !unit.file_name().and_then(|s| s.to_str()).is_some_and(hash) {
                continue;
            }
            let compile = unit.join("out/build_script_build").is_file()
                || unit.join("out/build_script_build.exe").is_file();
            let run = unit.join("run/invoked.timestamp").is_file();
            if compile || run {
                units.push((name.to_owned(), unit, compile));
            }
        }
    }
    Ok(units)
}

/// A build-script variant or incremental dir untouched for this long is evictable:
/// a build that holds one rewrites its mtime well inside the hour.
pub(crate) const STALE_AFTER: Duration = Duration::from_secs(3600);

/// Includes target-triple/profile layouts; never follows directory symlinks.
pub(crate) fn candidates(root: &Path, now: SystemTime) -> io::Result<Vec<(PathBuf, u64)>> {
    let mut profiles = directories(root)?;
    for child in profiles.clone() {
        profiles.extend(directories(&child)?.into_iter().filter(|p| {
            matches!(
                p.file_name().and_then(|s| s.to_str()),
                Some("debug" | "release")
            )
        }));
    }
    let mut candidates = vec![];
    for profile in profiles {
        for kind in ["incremental", "build"] {
            let parent = profile.join(kind);
            if parent.is_symlink() {
                continue;
            }
            let mut crates: BTreeMap<(String, bool), Vec<_>> = BTreeMap::new();
            if kind == "incremental" {
                for path in directories(&parent)? {
                    let (size, newest) = crate::disk_hygiene::scan(&path)?;
                    candidates.push((newest, path, size));
                }
            } else {
                for (name, path, compile) in build_units(&parent)? {
                    let (size, newest) = crate::disk_hygiene::scan(&path)?;
                    crates
                        .entry((name, compile))
                        .or_default()
                        .push((newest, path, size));
                }
            }
            for variants in crates.values_mut() {
                variants.sort();
                variants.pop(); // Retain newest of each kind, even when all are old.
                candidates.append(variants);
            }
        }
    }
    candidates
        .retain(|(newest, _, _)| now.duration_since(*newest).unwrap_or_default() > STALE_AFTER);
    candidates.sort();
    Ok(candidates
        .into_iter()
        .map(|(_, path, size)| (path, size))
        .collect())
}

#[cfg(target_os = "linux")]
pub struct Candidate {
    pub path: PathBuf,
    pub boundary: PathBuf,
    pub scope: PathBuf,
    pub lease_root: PathBuf,
    pub(crate) cutoff: SystemTime,
    pub(crate) identity: (u64, u64),
}

#[cfg(target_os = "linux")]
impl Candidate {
    /// Describe a provider-approved removal unit. Its identity must survive until unlink.
    pub fn new(path: PathBuf, boundary: PathBuf, cutoff: SystemTime) -> io::Result<Self> {
        let identity = crate::disk_hygiene::file_identity(&fs::symlink_metadata(&path)?);
        Ok(Self {
            scope: path.clone(),
            lease_root: path.clone(),
            path,
            boundary,
            cutoff,
            identity,
        })
    }

    pub(crate) fn safe(&self, uid: u32) -> Result<(), &'static str> {
        use std::os::unix::fs::MetadataExt;
        let meta = fs::symlink_metadata(&self.path).map_err(|e| match e.kind() {
            io::ErrorKind::NotFound => "vanished",
            _ => "probe_unavailable",
        })?;
        if !meta.is_dir()
            || crate::disk_hygiene::file_identity(&meta) != self.identity
            || meta.uid() != uid
            || !self.path.starts_with(&self.boundary)
            || self.path == self.boundary
            || self.path.canonicalize().as_ref().ok() != Some(&self.path)
            || crate::cargo_pool_trim::mount_guard(&self.boundary, &self.path).is_err()
        {
            return Err("unsafe");
        }
        Ok(())
    }

    pub(crate) fn lease(&self) -> Result<crate::cargo_cache::ExclusiveLease, &'static str> {
        crate::cargo_cache::try_exclusive_lease(&self.lease_root)
            .map_err(|_| "probe_unavailable")?
            .ok_or("leased")
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn nested_units_keep_newest_compile_and_run_and_unknown_artifacts() {
        let tmp = tempfile::tempdir().unwrap();
        let now = SystemTime::now();
        let mut expected = vec![];
        for profile in ["debug", "x86_64-unknown-linux-gnu/release"] {
            for (crate_name, unit, marker, age, evict) in [
                (
                    "some-crate",
                    "1111111111111111",
                    "out/build_script_build",
                    90000,
                    true,
                ),
                (
                    "some-crate",
                    "2222222222222222",
                    "out/build_script_build",
                    80000,
                    false,
                ),
                (
                    "some-crate",
                    "3333333333333333",
                    "run/invoked.timestamp",
                    90000,
                    true,
                ),
                (
                    "some-crate",
                    "4444444444444444",
                    "run/invoked.timestamp",
                    80000,
                    false,
                ),
                (
                    "other-crate",
                    "1111111111111111",
                    "out/build_script_build.exe",
                    90000,
                    false,
                ),
                (
                    "some-crate",
                    "5555555555555555",
                    "out/libsome_crate.rlib",
                    90000,
                    false,
                ),
                (
                    "some-crate",
                    "not-a-hash",
                    "run/invoked.timestamp",
                    90000,
                    false,
                ),
                (
                    "recent-crate",
                    "1111111111111111",
                    "run/invoked.timestamp",
                    10,
                    false,
                ),
                (
                    "recent-crate",
                    "2222222222222222",
                    "run/invoked.timestamp",
                    5,
                    false,
                ),
            ] {
                let path = tmp
                    .path()
                    .join(profile)
                    .join("build")
                    .join(crate_name)
                    .join(unit);
                let file = path.join(marker);
                fs::create_dir_all(file.parent().unwrap()).unwrap();
                fs::write(&file, [0; 42]).unwrap();
                let modified = now - Duration::from_secs(age);
                for p in [&file, file.parent().unwrap(), &path] {
                    fs::File::open(p).unwrap().set_modified(modified).unwrap();
                }
                if evict {
                    expected.push((path, 42));
                }
            }
            let outside = tempfile::tempdir().unwrap();
            std::os::unix::fs::symlink(
                outside.path(),
                tmp.path().join(profile).join("build/linked-crate"),
            )
            .unwrap();
            std::os::unix::fs::symlink(
                outside.path(),
                tmp.path()
                    .join(profile)
                    .join("build/some-crate/6666666666666666"),
            )
            .unwrap();
        }
        let mut rows = candidates(tmp.path(), now).unwrap();
        rows.sort();
        expected.sort();
        assert_eq!(rows, expected);
    }

    #[test]
    fn fifty_variants_past_the_hour_go_oldest_first_and_a_running_build_keeps_its_own() {
        let tmp = tempfile::tempdir().unwrap();
        let now = SystemTime::now();
        let mut stale = vec![];
        for n in 0..50u64 {
            let age = (61 + n) * 60;
            let name = format!("pg-worker-core-{n:016x}");
            let path = tmp.path().join("debug/build").join(&name);
            fs::create_dir_all(&path).unwrap();
            fs::write(path.join("build-script-build"), [0; 42]).unwrap();
            let modified = now - Duration::from_secs(age);
            for p in [path.join("build-script-build"), path.clone()] {
                fs::File::open(p).unwrap().set_modified(modified).unwrap();
            }
            stale.push(path);
        }
        // A build still running rewrote its unit minutes ago: never a candidate.
        let live = tmp
            .path()
            .join("debug/build/pg-worker-core-ffffffffffffffff");
        fs::create_dir_all(&live).unwrap();
        fs::write(live.join("build-script-build"), [0; 42]).unwrap();
        let rows = candidates(tmp.path(), now).unwrap();
        // The live unit is the crate's retained newest; all 50 stale ones go, oldest first.
        let expected: Vec<_> = stale.iter().rev().cloned().collect();
        assert_eq!(
            rows.iter().map(|r| r.0.clone()).collect::<Vec<_>>(),
            expected
        );
        assert!(rows.iter().all(|(p, _)| p != &live));
    }

    #[test]
    fn compile_and_run_units_of_current_build_both_survive() {
        let tmp = tempfile::tempdir().unwrap();
        let now = SystemTime::now();
        for (name, marker, age) in [
            ("libc-1111111111111111", "build-script-build", 90000),
            ("libc-2222222222222222", "output", 80000),
            ("unknown-1111111111111111", "private", 90000),
            ("unknown-2222222222222222", "private", 80000),
        ] {
            let path = tmp.path().join("debug/build").join(name);
            fs::create_dir_all(&path).unwrap();
            fs::write(path.join(marker), [0; 42]).unwrap();
            let modified = now - Duration::from_secs(age);
            for p in [path.join(marker), path] {
                fs::File::open(p).unwrap().set_modified(modified).unwrap();
            }
        }
        assert!(candidates(tmp.path(), now).unwrap().is_empty());
    }
    #[test]
    fn stale_build_variants_keep_newest_per_crate_and_profile() {
        let tmp = tempfile::tempdir().unwrap();
        let now = SystemTime::now();
        for profile in ["debug", "x86_64-unknown-linux-gnu/release"] {
            for (name, age) in [
                ("some-crate-1111111111111111", 90000),
                ("some-crate-2222222222222222", 80000),
                ("other-crate-1111111111111111", 90000),
                ("recent-crate-1111111111111111", 10),
                ("recent-crate-2222222222222222", 5),
                ("unrecognized-name", 90000),
            ] {
                let path = tmp.path().join(profile).join("build").join(name);
                fs::create_dir_all(&path).unwrap();
                fs::write(path.join("output"), [0; 42]).unwrap();
                let modified = now - Duration::from_secs(age);
                for p in [path.join("output"), path] {
                    fs::File::open(p).unwrap().set_modified(modified).unwrap();
                }
            }
        }
        let rows = candidates(tmp.path(), now).unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows
            .iter()
            .all(|(p, size)| p.ends_with("some-crate-1111111111111111") && *size == 42));
        #[cfg(unix)]
        {
            let outside = tempfile::tempdir().unwrap();
            std::os::unix::fs::symlink(
                outside.path(),
                tmp.path().join("debug/build/link-1111111111111111"),
            )
            .unwrap();
            assert_eq!(candidates(tmp.path(), now).unwrap().len(), 2);
        }
    }
}
