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
            for path in directories(&parent)? {
                let (size, newest) = crate::disk_hygiene::scan(&path)?;
                if kind == "incremental" {
                    candidates.push((newest, path, size));
                } else if let Some((name, hash)) = path
                    .file_name()
                    .and_then(|s| s.to_str())
                    .and_then(|s| s.rsplit_once('-'))
                {
                    // Cargo's compile and run units have different hashes even
                    // for the same current build. Retain the newest of each kind.
                    let compile = path.join("build-script-build").is_file()
                        || path.join("build-script-build.exe").is_file();
                    let run =
                        path.join("output").is_file() || path.join("invoked.timestamp").is_file();
                    if hash.len() == 16
                        && hash.bytes().all(|b| b.is_ascii_hexdigit())
                        && (compile || run)
                    {
                        crates
                            .entry((name.to_owned(), compile))
                            .or_default()
                            .push((newest, path, size));
                    }
                }
            }
            for variants in crates.values_mut() {
                variants.sort();
                variants.pop(); // Retain newest of each kind, even when all are old.
                candidates.append(variants);
            }
        }
    }
    candidates.retain(|(newest, _, _)| {
        now.duration_since(*newest).unwrap_or_default() > Duration::from_secs(6 * 3600)
    });
    candidates.sort();
    Ok(candidates
        .into_iter()
        .map(|(_, path, size)| (path, size))
        .collect())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
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
