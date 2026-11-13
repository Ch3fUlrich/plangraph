//! Fleet portion of host-sync. Kept separate from host-specific synchronization.
use std::path::{Path, PathBuf};

pub const REQUIRED: [&str; 3] = ["handoff-check", "resume-digest", "fleet-name"];

pub fn find(name: &str, dirs: &[PathBuf]) -> Option<PathBuf> {
    dirs.iter()
        .flat_map(|p| {
            #[cfg(windows)]
            let names = vec![
                p.join(format!("{name}.exe")),
                p.join(format!("{name}.cmd")),
                p.join(name),
            ];
            #[cfg(not(windows))]
            let names = vec![p.join(name)];
            names
        })
        .find(|p| {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                p.is_file()
                    && p.metadata()
                        .is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
            }
            #[cfg(not(unix))]
            {
                p.is_file()
            }
        })
}

/// Correct PATH is a no-op. A repair destination must already be on PATH.
pub fn check(dirs: &[PathBuf], source: Option<&Path>, dest: Option<&Path>) -> Vec<String> {
    let mut failures = vec![];
    for name in REQUIRED {
        if find(name, dirs).is_some() {
            continue;
        }
        let repair = || -> Result<(), String> {
            let source = source.ok_or("no fleet source directory configured")?;
            let dest = dest
                .filter(|d| dirs.iter().any(|p| p == *d))
                .ok_or("repair directory is not on PATH")?;
            let src = find(name, &[source.to_owned()])
                .ok_or("source command is missing or not executable")?;
            std::fs::create_dir_all(dest).map_err(|e| e.to_string())?;
            let target = dest.join(src.file_name().ok_or("source has no filename")?);
            #[cfg(unix)]
            std::os::unix::fs::symlink(
                std::fs::canonicalize(src).map_err(|e| e.to_string())?,
                &target,
            )
            .map_err(|e| e.to_string())?;
            #[cfg(windows)]
            std::fs::copy(src, &target)
                .map_err(|e| e.to_string())
                .map(|_| ())?;
            if find(name, dirs).is_none() {
                return Err("command still does not resolve on PATH".into());
            }
            Ok(())
        };
        if let Err(e) = repair() {
            failures.push(format!("FAIL fleet PATH: {name}: {e}"));
        }
    }
    failures
}
