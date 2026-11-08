//! Launch assembly only; lifecycle and destructive operations belong to R5.
use crate::{
    hosts::RepoEntry,
    lockfile,
    workspace::{Mode, Workspace},
    workspace_resolver::{ensure_commit, git_command, ResolveError},
    workspace_verify::{configure_import_environment, verify_workspace, VerifiedWorkspace},
};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

fn require(ok: bool, reason: &'static str) -> Result<(), ResolveError> {
    if ok {
        Ok(())
    } else {
        Err(ResolveError::Verify(reason))
    }
}

fn safe_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 80
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
}

fn git(root: &Path, args: &[&str]) -> Result<String, ResolveError> {
    let out = git_command(root).args(args).output()?;
    require(out.status.success(), "git")?;
    Ok(String::from_utf8(out.stdout)
        .map_err(|_| ResolveError::Git)?
        .trim_end_matches(['\r', '\n'])
        .into())
}

fn git_path(root: &Path, arg: &str) -> Result<PathBuf, ResolveError> {
    Ok(root.join(git(root, &["rev-parse", arg])?).canonicalize()?)
}

fn identity(repo: &RepoEntry, checkout: &Path) -> Result<(PathBuf, PathBuf), ResolveError> {
    let checkout = checkout.canonicalize()?;
    let root = Path::new(&repo.root).canonicalize()?;
    let trees = Path::new(&repo.worktrees_root).canonicalize()?;
    require(
        checkout != trees && checkout.starts_with(&trees) && checkout != root,
        "worktree_location",
    )?;
    require(
        git_path(&checkout, "--show-toplevel")? == checkout,
        "toplevel",
    )?;
    let common = git_path(&root, "--git-common-dir")?;
    require(
        git_path(&checkout, "--git-common-dir")? == common,
        "common_dir",
    )?;
    let admin = git_path(&checkout, "--absolute-git-dir")?;
    require(
        admin != common.join("worktrees") && admin.starts_with(common.join("worktrees")),
        "linked_worktree",
    )?;
    require(
        git(&checkout, &["remote", "get-url", "--all", "origin"])? == repo.remote,
        "remote",
    )?;
    Ok((checkout, admin))
}

// RepoEntry has no id field: canonical registry common-dir plus registry root is
// its stable identity. Bind both, rather than trusting a caller-supplied card id.
fn binding(repo: &RepoEntry, checkout: &Path) -> Result<Vec<u8>, ResolveError> {
    serde_json::to_vec(&(Path::new(&repo.root).canonicalize()?, checkout))
        .map_err(|_| ResolveError::Invalid("marker_encoding"))
}

/// Proof independent of dirty HEAD. Call immediately before EVERY R5 destructive operation.
pub fn verify_owned(repo: &RepoEntry, checkout: &Path) -> Result<(), ResolveError> {
    let (checkout, admin) = identity(repo, checkout)?;
    let marker = admin.join("spawner-owned");
    require(
        fs::symlink_metadata(&marker)?.file_type().is_file(),
        "marker_type",
    )?;
    require(
        fs::read(marker)? == binding(repo, &checkout)?,
        "marker_binding",
    )
}

/// Fresh lane branch only; failures leave explicit partial state, never remove it.
pub fn create_writer(
    repo: &RepoEntry,
    workspace: &Workspace,
    run_id: &str,
) -> Result<PathBuf, ResolveError> {
    workspace
        .validate()
        .map_err(|_| ResolveError::Invalid("workspace"))?;
    require(
        workspace.mode == Mode::Writer && safe_id(run_id),
        "writer_run_id",
    )?;
    let base = workspace
        .base_sha
        .as_deref()
        .ok_or(ResolveError::Invalid("base"))?;
    ensure_commit(repo, base, Duration::from_secs(30))?;
    let trees = Path::new(&repo.worktrees_root).canonicalize()?;
    // safe_id admits only ASCII alphanumerics, '-' and '_': exactly one leaf,
    // with no traversal, trailing dot/space or drive-relative Windows syntax.
    let dest = trees.join(run_id);
    require(
        fs::symlink_metadata(&dest).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound),
        "destination_exists",
    )?;
    require(
        dest.parent()
            .ok_or(ResolveError::Invalid("parent"))?
            .canonicalize()?
            == trees,
        "parent",
    )?;
    let root = Path::new(&repo.root);
    let common = git_path(root, "--git-common-dir")?;
    let _guard = lockfile::lock_with_retry(
        &common.join("spawner-config.lock"),
        Duration::from_secs(30),
        &mut rand::rng(),
    )
    .map_err(|_| ResolveError::Lock)?;
    require(
        fs::symlink_metadata(&dest).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound),
        "destination_exists",
    )?;
    require(git_path(root, "--git-common-dir")? == common, "common_dir")?;
    git(
        root,
        &["config", "--local", "extensions.worktreeConfig", "true"],
    )?;
    let branch = workspace
        .branch
        .as_deref()
        .ok_or(ResolveError::Invalid("branch"))?;
    let out = git_command(root)
        .args([
            "-c",
            "core.hooksPath=",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.longpaths=true",
            "worktree",
            "add",
            "--no-checkout",
            "-b",
            branch,
            "--",
        ])
        .arg(git_worktree_path(&dest))
        .arg(base)
        .output()?;
    require(out.status.success(), "worktree_add")?;
    let (checkout, admin) = identity(repo, &dest)?;
    git(
        &checkout,
        &["config", "--worktree", "core.autocrlf", "false"],
    )?;
    #[cfg(windows)]
    git(
        &checkout,
        &["config", "--worktree", "core.longpaths", "true"],
    )?;
    git(&checkout, &["-c", "core.hooksPath=", "checkout", branch])?;
    let mut marker = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(admin.join("spawner-owned"))?;
    marker.write_all(&binding(repo, &checkout)?)?;
    Ok(checkout)
}

// Git for Windows does not accept Rust's canonical \\?\ worktree arguments.
pub(crate) fn git_worktree_path(path: &Path) -> std::path::PathBuf {
    #[cfg(windows)]
    {
        use std::path::{Component, Prefix};
        let mut components = path.components();
        let prefix = match components.next() {
            Some(Component::Prefix(prefix)) => prefix,
            _ => return path.to_path_buf(),
        };
        if components.next() != Some(Component::RootDir) {
            return path.to_path_buf();
        }
        let mut native = std::ffi::OsString::new();
        match prefix.kind() {
            Prefix::VerbatimDisk(drive) => native.push(format!("{}:", char::from(drive))),
            Prefix::VerbatimUNC(server, share) => {
                native.push("\\\\");
                native.push(server);
                native.push("\\");
                native.push(share);
            }
            _ => return path.to_path_buf(),
        }
        native.push("\\");
        let mut result = std::path::PathBuf::from(native);
        for component in components {
            result.push(component.as_os_str());
        }
        result
    }
    #[cfg(not(windows))]
    path.to_path_buf()
}

fn strip_git(cmd: &mut Command) {
    let keys: Vec<_> = std::env::vars_os()
        .map(|(k, _)| k)
        .chain(cmd.get_envs().map(|(k, _)| k.to_os_string()))
        .collect();
    for key in keys {
        let upper = key.to_string_lossy().to_ascii_uppercase();
        if upper.starts_with("GIT_")
            || [
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "CREDENTIAL",
                "API_KEY",
                "PRIVATE_KEY",
            ]
            .iter()
            .any(|part| upper.contains(part))
        {
            cmd.env_remove(key);
        }
    }
}

// Resolve existing ancestors even when the slot's cache directory is not created yet.
fn resolved(path: &Path) -> Result<PathBuf, ResolveError> {
    require(
        path.is_absolute()
            && !path
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir)),
        "cache_path",
    )?;
    if path.try_exists()? {
        return Ok(path.canonicalize()?);
    }
    require(fs::symlink_metadata(path).is_err(), "cache_symlink")?;
    Ok(
        resolved(path.parent().ok_or(ResolveError::Invalid("cache_root"))?)?.join(
            path.file_name()
                .ok_or(ResolveError::Invalid("cache_name"))?,
        ),
    )
}

/// Overlay the harness-confined command; never restore credentials removed by confinement.
pub fn apply_child_env(
    cmd: &mut Command,
    repo: &RepoEntry,
    checkout: &Path,
    slot: &str,
    temp: &Path,
) -> Result<(), ResolveError> {
    require(safe_id(slot), "slot")?;
    let checkout = checkout.canonicalize()?;
    let trees = Path::new(&repo.worktrees_root).canonicalize()?;
    let temp = temp.canonicalize()?;
    require(!temp.starts_with(&trees), "temp_escape")?;
    // Preserve explicit removals even for allowlisted names when reconfining.
    let removed: Vec<_> = cmd
        .get_envs()
        .filter(|(_, value)| value.is_none())
        .map(|(key, _)| key.to_os_string())
        .collect();
    crate::sandbox_env::confine_check(cmd, &temp);
    for key in removed {
        cmd.env_remove(key);
    }
    configure_import_environment(cmd, repo, &checkout)?;
    strip_git(cmd);
    cmd.env("TMPDIR", &temp)
        .env("TEMP", &temp)
        .env("TMP", &temp)
        .env("PYTEST_ADDOPTS", "-p no:cacheprovider");
    for (key, template) in &repo.cache_env {
        let upper = key.to_ascii_uppercase();
        require(
            !key.is_empty()
                && key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                && !upper.starts_with("GIT_")
                && !upper.starts_with("PYTHON")
                && !upper.starts_with("DYLD_")
                && ![
                    "PWD",
                    "TMPDIR",
                    "TEMP",
                    "TMP",
                    "PYTEST_ADDOPTS",
                    "PATH",
                    "HOME",
                    "LD_PRELOAD",
                    "LD_LIBRARY_PATH",
                    "LD_AUDIT",
                    "RUSTC_WRAPPER",
                    "BASH_ENV",
                    "SYSTEMROOT",
                    "COMSPEC",
                ]
                .contains(&upper.as_str()),
            "cache_key",
        )?;
        let expanded = template.replace("%SLOT%", slot);
        require(!expanded.contains('%'), "cache_token")?;
        let path = resolved(Path::new(&expanded))?;
        let anchor = template.split("%SLOT%").next().unwrap_or(template);
        let anchor = if template.contains("%SLOT%") {
            Path::new(anchor)
        } else {
            Path::new(template)
                .parent()
                .ok_or(ResolveError::Invalid("cache_anchor"))?
        };
        let anchor = resolved(anchor)?;
        require(
            path.starts_with(&anchor) && !path.starts_with(&trees) && !anchor.starts_with(&trees),
            "cache_escape",
        )?;
        cmd.env(key, path);
    }
    Ok(())
}

/// Reverify immediately before spawning. None mode has no implicit checkout.
#[allow(clippy::too_many_arguments)]
pub fn prepare_launch(
    cmd: &mut Command,
    repo: &RepoEntry,
    workspace: &Workspace,
    checkout: Option<&Path>,
    slot: &str,
    temp: &Path,
    import_package: Option<&str>,
) -> Result<Option<VerifiedWorkspace>, ResolveError> {
    workspace
        .validate()
        .map_err(|_| ResolveError::Invalid("workspace"))?;
    if workspace.mode == Mode::None {
        require(checkout.is_none(), "none_checkout")?;
        require(Path::new(&repo.worktrees_root).is_dir(), "worktrees_root")?;
        let trees = resolved(Path::new(&repo.worktrees_root))?;
        require(temp.is_absolute() && !temp.starts_with(&trees), "temp")?;
        require(!temp.canonicalize()?.starts_with(&trees), "temp_escape")?;
        crate::sandbox_env::confine_check(cmd, temp);
        strip_git(cmd);
        cmd.current_dir(temp).env("PWD", temp);
        return Ok(None);
    }
    let checkout = checkout.ok_or(ResolveError::Invalid("checkout"))?;
    if workspace.mode == Mode::Writer {
        verify_owned(repo, checkout)?;
    }
    apply_child_env(cmd, repo, checkout, slot, temp)?;
    let verified = verify_workspace(repo, workspace, checkout, import_package)?;
    Ok(Some(verified))
}

#[cfg(test)]
#[path = "workspace_launch_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "workspace_launch_env_tests.rs"]
mod env_tests;

#[cfg(test)]
#[path = "workspace_launch_path_tests.rs"]
mod path_tests;
