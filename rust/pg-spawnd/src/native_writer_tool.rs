//! Resolve the native writer executable before starting a run.
use std::{
    fs,
    path::{Path, PathBuf},
};

fn windows_mount(path: &Path, is_wsl: bool, mountinfo: &str) -> bool {
    if !is_wsl {
        return false;
    }
    let Ok(relative) = path.strip_prefix("/mnt") else {
        return false;
    };
    // mountinfo octal-escapes these four characters in mount point paths.
    let escaped = path
        .to_string_lossy()
        .replace('\\', "\\134")
        .replace(' ', "\\040")
        .replace('\t', "\\011")
        .replace('\n', "\\012");
    let mount = mountinfo
        .lines()
        .filter_map(|line| {
            let (fields, fs) = line.split_once(" - ")?;
            let point = Path::new(fields.split_whitespace().nth(4)?);
            (point.starts_with("/mnt") && Path::new(&escaped).starts_with(point))
                .then_some((point.as_os_str().len(), fs.split_whitespace().next()?))
        })
        .max_by_key(|&(length, _)| length);
    match mount {
        Some((_, filesystem)) => matches!(filesystem, "drvfs" | "9p"),
        None => relative.iter().next().is_some_and(
            |drive| matches!(drive.as_encoded_bytes(), [letter] if letter.is_ascii_alphabetic()),
        ),
    }
}

fn require_native(path: &Path, is_wsl: bool, mountinfo: &str) -> Result<(), String> {
    if windows_mount(path, is_wsl, mountinfo) {
        return Err(format!(
            "tool_wrong_platform: codex at {} is on a WSL Windows drive; export PATH=\"$HOME/.local/bin:$PATH\" with Linux Codex installed",
            path.display()
        ));
    }
    Ok(())
}

fn checked_path(
    path: PathBuf,
    check: impl Fn(&Path) -> Result<(), String>,
) -> Result<PathBuf, String> {
    check(&path)?;
    let canonical = fs::canonicalize(&path).map_err(|e| format!("tool_missing: codex: {e}"))?;
    check(&canonical)?;
    // Preserve npm's selected symlink path while checking its actual target.
    Ok(path)
}

pub(crate) fn resolve(path: &std::ffi::OsStr, cwd: &Path) -> Result<PathBuf, String> {
    for directory in std::env::split_paths(path) {
        let candidate = cwd.join(directory).join("codex");
        if !candidate.is_file() {
            continue;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if candidate
                .metadata()
                .map_err(|e| e.to_string())?
                .permissions()
                .mode()
                & 0o111
                == 0
            {
                continue;
            }
        }
        let is_wsl = cfg!(target_os = "linux")
            && pg_worker_core::wsl::is_wsl(
                &fs::read_to_string("/proc/sys/kernel/osrelease").unwrap_or_default(),
            );
        let mountinfo = if is_wsl {
            fs::read_to_string("/proc/self/mountinfo").unwrap_or_default()
        } else {
            String::new()
        };
        return checked_path(
            std::path::absolute(candidate).map_err(|e| e.to_string())?,
            |path| require_native(path, is_wsl, &mountinfo),
        );
    }
    Err("tool_missing: codex not found on PATH".into())
}

#[cfg(test)]
#[path = "native_writer_tool_tests.rs"]
mod tests;
