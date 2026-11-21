//! Sandbox and environment of the native Codex writer child (AGENT-JAIL step 1).
//!
//! The writer runs `sandbox_mode = "workspace-write"` with network access on (it fetches, builds
//! and starts its own reviewers) and a named list of extra writable roots; its environment is an
//! allowlist, so a host secret that is not named here never reaches Codex or its tools.
use std::{
    ffi::{OsStr, OsString},
    path::{Path, PathBuf},
    process::Command,
};

/// Variables the writer child receives. `PATH` is set separately from the admitted tool path.
/// Harness auth stays (`CODEX_HOME`, `OPENAI_*`); git, ssh, proxy and TLS settings stay because the
/// writer fetches; build variables stay because it builds.
pub(crate) const ENV_ALLOW: &[&str] = &[
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LANGUAGE",
    "TZ",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN",
    "SSH_AUTH_SOCK",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "PLANGRAPH_CARGO_TARGET_DIR",
];

fn allowed(name: &OsStr) -> bool {
    name.to_str()
        .is_some_and(|n| ENV_ALLOW.contains(&n) || n.starts_with("LC_"))
}

/// The inherited variables the writer child may keep.
pub(crate) fn allowed_env(
    inherited: impl IntoIterator<Item = (OsString, OsString)>,
) -> Vec<(OsString, OsString)> {
    inherited
        .into_iter()
        .filter(|(name, _)| allowed(name))
        .collect()
}

/// Replace the command's environment with the allowlisted `inherited` variables and `path`.
pub(crate) fn apply_env(
    command: &mut Command,
    inherited: impl IntoIterator<Item = (OsString, OsString)>,
    path: &str,
) {
    command
        .env_clear()
        .envs(allowed_env(inherited))
        .env("PATH", path);
}

/// The git directories the writer's commits land in: the repository's common directory and the
/// worktree's own directory (Codex keeps a worktree's directory read-only unless it is named).
pub(crate) fn git_dirs(cwd: &Path, path: &str) -> Result<Vec<PathBuf>, String> {
    let out = Command::new("git")
        .env("PATH", path)
        .arg("-C")
        .arg(cwd)
        .args([
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "--absolute-git-dir",
        ])
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "git directory lookup failed: {}",
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(PathBuf::from)
        .collect())
}

/// Extra writable roots: the shared cargo target, the review directory, the run directory and the
/// git directories (the worktree's commits land there). The worktree itself is the workspace root
/// and is writable by the mode.
pub(crate) fn writable_roots(
    cargo_target: &Path,
    reviews: &Path,
    run_dir: &Path,
    git_dirs: &[PathBuf],
) -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = Vec::new();
    for root in [cargo_target, reviews, run_dir]
        .into_iter()
        .chain(git_dirs.iter().map(PathBuf::as_path))
    {
        if !roots.iter().any(|known| known == root) {
            roots.push(root.to_path_buf());
        }
    }
    roots
}

/// `-c` overrides that select the mode, keep network access on and name the writable roots.
pub(crate) fn sandbox_args(roots: &[PathBuf]) -> Result<Vec<String>, String> {
    let list = roots
        .iter()
        .map(|root| {
            let text = root
                .to_str()
                .ok_or_else(|| format!("writable root is not UTF-8: {}", root.display()))?;
            serde_json::to_string(text).map_err(|e| e.to_string())
        })
        .collect::<Result<Vec<_>, _>>()?
        .join(",");
    Ok([
        format!(
            "sandbox_mode=\"{}\"",
            pg_worker_core::host_readiness::WRITER_SANDBOX
        ),
        "sandbox_workspace_write.network_access=true".to_owned(),
        format!("sandbox_workspace_write.writable_roots=[{list}]"),
    ]
    .into_iter()
    .flat_map(|value| ["-c".to_owned(), value])
    .collect())
}

#[cfg(test)]
#[path = "native_writer_sandbox_tests.rs"]
mod tests;
