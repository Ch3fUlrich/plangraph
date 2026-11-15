//! Resolve writer prerequisites in the target host's effective environment.
use serde_json::{json, Value};
use std::path::Path;

fn executable(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::{ffi::CString, os::unix::ffi::OsStrExt};
        CString::new(path.as_os_str().as_bytes())
            .is_ok_and(|path| unsafe { libc::access(path.as_ptr(), libc::X_OK) == 0 })
    }
    #[cfg(not(unix))]
    {
        true
    }
}

pub(crate) fn snapshot() -> Result<Value, String> {
    let path = std::env::var_os("PATH").ok_or("tool_missing: cargo (PATH unset)")?;
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    // Relative/empty entries must keep their caller meaning after worktree creation.
    let directories: Vec<_> = std::env::split_paths(&path)
        .map(|entry| cwd.join(entry))
        .collect();
    let path = std::env::join_paths(&directories)
        .map_err(|e| e.to_string())?
        .into_string()
        .map_err(|_| "native tool PATH must be UTF-8")?;
    let mut tools = json!({});
    for tool in ["cargo", "git", "codex"] {
        let resolved = directories
            .iter()
            .map(|directory| directory.join(tool))
            .find(|path| executable(path))
            .ok_or_else(|| format!("tool_missing: {tool}"))?;
        // Retain the invocation path: canonicalizing rustup's cargo link changes argv[0].
        tools[tool] = json!(resolved);
    }
    Ok(json!({"tool_path":path, "resolved_tools":tools}))
}
