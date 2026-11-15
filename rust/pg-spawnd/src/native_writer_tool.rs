//! Resolve the native writer executable before starting a run.
use std::{
    fs,
    path::{Path, PathBuf},
};

fn require_native(path: &Path) -> Result<(), String> {
    if path.starts_with("/mnt") {
        return Err(format!(
            "tool_wrong_platform: codex at {} is under /mnt/; export PATH=\"$HOME/.local/bin:$PATH\" with Linux Codex installed",
            path.display()
        ));
    }
    Ok(())
}

fn checked_path(path: PathBuf) -> Result<PathBuf, String> {
    require_native(&path)?;
    let canonical = fs::canonicalize(&path).map_err(|e| format!("tool_missing: codex: {e}"))?;
    require_native(&canonical)?;
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
        return checked_path(std::path::absolute(candidate).map_err(|e| e.to_string())?);
    }
    Err("tool_missing: codex not found on PATH".into())
}

#[cfg(test)]
#[path = "native_writer_tool_tests.rs"]
mod tests;
