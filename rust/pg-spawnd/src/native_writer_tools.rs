//! Resolve writer prerequisites in the target host's effective environment.
use pg_worker_core::host_readiness::{Native, Probe, Runner, SandboxStatus, WRITER_SANDBOX};
use serde_json::{json, Value};
use std::{
    ffi::OsStr,
    path::{Path, PathBuf},
};

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

pub(crate) fn resolve_on_path(path: &OsStr, cwd: &Path, program: &str) -> Result<PathBuf, String> {
    let executable_path = if Path::new(program).components().count() > 1 {
        let candidate = cwd.join(program);
        executable(&candidate).then_some(candidate)
    } else {
        std::env::split_paths(path)
            .map(|directory| cwd.join(directory).join(program))
            .find(|candidate| executable(candidate))
    };
    executable_path.ok_or_else(|| format!("tool_missing: {program}"))
}

pub(crate) fn snapshot(check_sandbox: bool) -> Result<Value, String> {
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
    for tool in ["cargo", "git"] {
        let resolved = resolve_on_path(path.as_ref(), &cwd, tool)?;
        // Retain the invocation path: canonicalizing rustup's cargo link changes argv[0].
        tools[tool] = json!(resolved);
    }
    // Codex also refuses Windows-side installs at admission, with the launch-time rule.
    let codex = crate::native_writer_tool::resolve(path.as_ref(), &cwd)?;
    tools["codex"] = json!(codex);
    let mut report = json!({"tool_path":path, "resolved_tools":tools});
    if check_sandbox {
        let result = (|| {
            let scratch = tempfile::tempdir()?;
            let mut runner = Native {
                codex,
                executable: std::env::current_exe()?,
                tool_path: Some(path.clone()),
            };
            runner.probe(Probe::Sandbox, scratch.path(), WRITER_SANDBOX)
        })();
        let sandbox =
            serde_json::to_value(SandboxStatus::from_probe(result)).map_err(|e| e.to_string())?;
        report
            .as_object_mut()
            .unwrap()
            .extend(sandbox.as_object().unwrap().clone());
    }
    Ok(report)
}
