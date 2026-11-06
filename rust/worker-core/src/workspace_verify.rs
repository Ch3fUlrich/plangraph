//! Non-mutating pre-launch verification. A token is not a same-user TOCTOU boundary.
use crate::hosts::RepoEntry;
use crate::stall::{self, Opts};
use crate::workspace::{Mode, Workspace};
use crate::workspace_resolver::{git_command, ResolveError};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

/// Only successful verification can construct this identity snapshot.
#[derive(Debug)]
pub struct VerifiedWorkspace {
    checkout: PathBuf,
    common_dir: PathBuf,
    admin_dir: PathBuf,
    revision: String,
}

impl VerifiedWorkspace {
    pub fn checkout(&self) -> &Path {
        &self.checkout
    }
    pub fn common_dir(&self) -> &Path {
        &self.common_dir
    }
    pub fn admin_dir(&self) -> &Path {
        &self.admin_dir
    }
    pub fn revision(&self) -> &str {
        &self.revision
    }
}

fn require(ok: bool, reason: &'static str) -> Result<(), ResolveError> {
    if ok {
        Ok(())
    } else {
        Err(ResolveError::Verify(reason))
    }
}

fn git(root: &Path, args: &[&str]) -> Result<String, ResolveError> {
    let out = git_command(root).args(args).output()?;
    require(out.status.success(), "git")?;
    String::from_utf8(out.stdout)
        .map(|s| s.trim_end_matches(['\r', '\n']).to_owned())
        .map_err(|_| ResolveError::Verify("git_encoding"))
}

fn git_path(root: &Path, arg: &str) -> Result<PathBuf, ResolveError> {
    Ok(root.join(git(root, &["rev-parse", arg])?).canonicalize()?)
}

fn inside(path: &Path, root: &Path) -> bool {
    path != root && path.starts_with(root)
}

/// Shared import environment policy for the canary and future launch assembly.
/// Roots must resolve inside the checkout; inherited git location overrides cannot leak.
pub fn configure_import_environment(
    cmd: &mut Command,
    repo: &RepoEntry,
    checkout: &Path,
) -> Result<(), ResolveError> {
    let mut roots = Vec::new();
    for root in &repo.import_roots {
        require(!Path::new(root).is_absolute(), "import_root_relative")?;
        let path = checkout.join(root).canonicalize()?;
        require(path == checkout || inside(&path, checkout), "import_root")?;
        roots.push(path);
    }
    let paths = std::env::join_paths(roots).map_err(|_| ResolveError::Verify("pythonpath"))?;
    let keys: Vec<_> = std::env::vars_os()
        .map(|(key, _)| key)
        .chain(cmd.get_envs().map(|(key, _)| key.to_os_string()))
        .collect();
    for key in keys {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            cmd.env_remove(key);
        }
    }
    cmd.current_dir(checkout)
        .env("PWD", checkout)
        .env("PYTHONPATH", paths)
        .env("PYTHONDONTWRITEBYTECODE", "1");
    Ok(())
}

fn canary(repo: &RepoEntry, checkout: &Path, package: &str) -> Result<(), ResolveError> {
    require(
        package.split('.').all(|part| {
            let mut chars = part.bytes();
            chars
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic() || c == b'_')
                && chars.all(|c| c.is_ascii_alphanumeric() || c == b'_')
        }),
        "import_package",
    )?;
    let python = repo.python.as_ref().ok_or(ResolveError::Verify("python"))?;
    let mut cmd = Command::new(python);
    // Path containment is checked in the child, not parsed from mixed stdout/stderr.
    cmd.args(["-c", "import importlib,pathlib,sys; m=importlib.import_module(sys.argv[1]); p=pathlib.Path(m.__file__).resolve(strict=True); r=pathlib.Path.cwd().resolve(strict=True); sys.exit(0 if p != r and r in p.parents else 1)", package]);
    let temp = std::env::temp_dir();
    crate::sandbox_env::confine_check(&mut cmd, &temp);
    crate::workspace_launch::apply_child_env(&mut cmd, repo, checkout, "canary", &temp)?;
    let out = stall::supervise(
        cmd,
        &Opts {
            timeout: Some(Duration::from_secs(10)),
            grace: Duration::from_secs(1),
            max_output: 4096,
            ..Opts::default()
        },
    )?;
    require(
        out.code == Some(0) && !out.timed_out && !out.stalled,
        "import_canary",
    )
}

/// Verify identity, isolation, revision, cleanliness, effective config and imports.
/// No creation, reset, cleanup, configuration writes or caller integration occurs here.
pub fn verify_workspace(
    repo: &RepoEntry,
    workspace: &Workspace,
    checkout: &Path,
    import_package: Option<&str>,
) -> Result<VerifiedWorkspace, ResolveError> {
    workspace
        .validate()
        .map_err(|_| ResolveError::Invalid("workspace"))?;
    require(workspace.mode != Mode::None, "none_mode")?;
    let checkout = checkout.canonicalize()?;
    let root = Path::new(&repo.root).canonicalize()?;
    let worktrees = Path::new(&repo.worktrees_root).canonicalize()?;
    require(
        inside(&checkout, &worktrees) && checkout != root,
        "worktree_location",
    )?;
    require(
        git_path(&checkout, "--show-toplevel")? == checkout,
        "toplevel",
    )?;
    let common_dir = git_path(&checkout, "--git-common-dir")?;
    require(
        common_dir == git_path(&root, "--git-common-dir")?,
        "common_dir",
    )?;
    let admin_dir = git_path(&checkout, "--absolute-git-dir")?;
    require(
        inside(&admin_dir, &common_dir.join("worktrees")),
        "linked_worktree",
    )?;
    require(
        git(&checkout, &["remote", "get-url", "--all", "origin"])? == repo.remote,
        "remote",
    )?;
    let revision = git(&checkout, &["rev-parse", "HEAD"])?;
    require(Some(&revision) == workspace.revision_sha.as_ref(), "head")?;
    require(
        git(
            &checkout,
            &["status", "--porcelain=v1", "--untracked-files=all"],
        )?
        .is_empty(),
        "dirty",
    )?;
    let branch = git(&checkout, &["rev-parse", "--symbolic-full-name", "HEAD"])?;
    let expected = workspace.branch.as_ref().map(|b| format!("refs/heads/{b}"));
    require(
        match workspace.mode {
            Mode::Writer => Some(&branch) == expected.as_ref(),
            Mode::Seat | Mode::Read => branch == "HEAD",
            Mode::None => false,
        },
        "branch",
    )?;
    require(
        git(&checkout, &["config", "--bool", "core.autocrlf"])? == "false",
        "autocrlf",
    )?;
    #[cfg(windows)]
    require(
        git(&checkout, &["config", "--bool", "core.longpaths"])? == "true",
        "longpaths",
    )?;
    require(
        import_package.is_some() || repo.python.is_none() || repo.import_roots.is_empty(),
        "import_package_required",
    )?;
    if let Some(package) = import_package {
        canary(repo, &checkout, package)?;
    }
    Ok(VerifiedWorkspace {
        checkout,
        common_dir,
        admin_dir,
        revision,
    })
}

#[cfg(test)]
#[path = "workspace_verify_tests.rs"]
mod tests;
