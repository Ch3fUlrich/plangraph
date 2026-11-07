//! Disposable seats use admission's grant, never a second allocation authority.
use crate::{
    hosts::RepoEntry,
    lockfile,
    slots::{self, SlotClass},
    workspace::{Mode, Workspace, WorkspaceRecord},
    workspace_launch::verify_owned,
    workspace_resolver::git_command,
    writer_result::{safe_id, WriterError},
};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};
pub struct SeatProof {
    pub checkout: PathBuf,
    pub repo_id: String,
    pub head_at_start: String,
    pub slot: u32,
    pub run_id: String,
    state: PathBuf,
    base: String,
}
pub(crate) fn git(path: &Path, args: &[&str]) -> Result<Vec<u8>, WriterError> {
    let out = git_command(path)
        .args(["-c", "core.hooksPath=", "-c", "submodule.recurse=false"])
        .args(args)
        .output()?;
    if !out.status.success() {
        return Err(WriterError::Git);
    }
    Ok(out.stdout)
}
pub(crate) fn common(repo: &RepoEntry) -> Result<PathBuf, WriterError> {
    let root = Path::new(&repo.root);
    let bytes = git(root, &["rev-parse", "--git-common-dir"])?;
    let text = std::str::from_utf8(&bytes).map_err(|_| WriterError::Git)?;
    Ok(root.join(text.trim()).canonicalize()?)
}
pub(crate) fn lock(path: &Path) -> Result<lockfile::LockGuard, WriterError> {
    lockfile::lock_with_retry(path, Duration::from_secs(30), &mut rand::rng())
        .map_err(|_| WriterError::Verify)
}
fn owned(repo: &RepoEntry, path: &Path) -> Result<(), WriterError> {
    if fs::symlink_metadata(path)?.file_type().is_symlink() {
        return Err(WriterError::Verify);
    }
    verify_owned(repo, path).map_err(|_| WriterError::Verify)
}
pub(crate) fn reset(repo: &RepoEntry, path: &Path, sha: &str) -> Result<(), WriterError> {
    owned(repo, path)?;
    git(path, &["checkout", "-f", "--detach", sha])?;
    owned(repo, path)?;
    git(path, &["clean", "-ffdx"])?;
    Ok(())
}
fn clean(repo: &RepoEntry, path: &Path, sha: &str) -> Result<(), WriterError> {
    owned(repo, path)?;
    let symbolic = git_command(path)
        .args(["symbolic-ref", "-q", "HEAD"])
        .output()?;
    if symbolic.status.code() != Some(1)
        || git(path, &["rev-parse", "HEAD"])? != format!("{sha}\n").as_bytes()
        || !git(
            path,
            &["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )?
        .is_empty()
    {
        return Err(WriterError::Verify);
    }
    Ok(())
}
// Same admin marker binding as R4; the marker is never inside the checkout.
fn create(repo: &RepoEntry, path: &Path, sha: &str) -> Result<(), WriterError> {
    let root = Path::new(&repo.root);
    let common = common(repo)?;
    let _config = lock(&common.join("spawner-config.lock"))?;
    git(
        root,
        &["config", "--local", "extensions.worktreeConfig", "true"],
    )?;
    let mut cmd = git_command(root);
    cmd.args(["-c", "core.hooksPath=", "-c", "submodule.recurse=false"]);
    let out = cmd
        .args(["worktree", "add", "--no-checkout", "--detach", "--"])
        .arg(path)
        .arg(sha)
        .output()?;
    if !out.status.success() {
        return Err(WriterError::Git);
    }
    let bytes = git(path, &["rev-parse", "--absolute-git-dir"])?;
    let text = std::str::from_utf8(&bytes).map_err(|_| WriterError::Git)?;
    let admin = path.join(text.trim()).canonicalize()?;
    if !admin.starts_with(common.join("worktrees")) {
        return Err(WriterError::Verify);
    }
    git(path, &["config", "--worktree", "core.autocrlf", "false"])?;
    #[cfg(windows)]
    git(path, &["config", "--worktree", "core.longpaths", "true"])?;
    let binding = serde_json::to_vec(&(root.canonicalize()?, path.canonicalize()?))
        .map_err(|_| WriterError::Invalid)?;
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(admin.join("spawner-owned"))?
        .write_all(&binding)?;
    Ok(())
}
#[rustfmt::skip]
pub fn prepare_seat(repo: &RepoEntry, workspace: &Workspace, state: &Path, run_id: &str, slot: u32) -> Result<SeatProof, WriterError> {
    workspace.validate().map_err(|_| WriterError::Invalid)?;
    if !safe_id(run_id) || slot == 0 || !matches!(workspace.mode, Mode::Seat | Mode::Read) {
        return Err(WriterError::Invalid);
    }
    let sha = workspace
        .revision_sha
        .as_deref()
        .ok_or(WriterError::Invalid)?;
    if !crate::writer_result::oid(sha) { return Err(WriterError::Invalid); }
    slots::with_table(state, Duration::from_secs(30), |table| {
        let grants: Vec<_> = table
            .active
            .iter()
            .filter(|a| a.repo_id == workspace.repo_id && a.seat_slot == Some(slot))
            .collect();
        if grants.len() != 1 || grants[0].run_id != run_id || grants[0].class != SlotClass::Seat {
            return Err(WriterError::Verify);
        }
        let checkout = Path::new(&repo.worktrees_root)
            .canonicalize()?
            .join(format!("seat-{slot:02}"));
        let _seat = lock(&common(repo)?.join(format!("spawner-seat-{slot}.lock")))?;
        match fs::symlink_metadata(&checkout) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => create(repo, &checkout, sha)?,
            Err(e) => return Err(e.into()),
            Ok(_) => (),
        }
        reset(repo, &checkout, sha)?;
        clean(repo, &checkout, sha)?;
        Ok(SeatProof {
            checkout,
            repo_id: workspace.repo_id.clone(),
            head_at_start: sha.into(),
            slot,
            run_id: run_id.into(),
            state: state.canonicalize()?,
            base: workspace.base_sha.clone().unwrap_or_else(|| sha.into()),
        })
    })
    .map_err(|_| WriterError::Verify)?
}
pub fn finish_seat(repo: &RepoEntry, proof: &SeatProof) -> Result<WorkspaceRecord, WriterError> {
    let _seat = lock(&common(repo)?.join(format!("spawner-seat-{}.lock", proof.slot)))?;
    let lifecycle = crate::writer_cleanup::load(&proof.state, &proof.run_id)?;
    if !lifecycle.group_empty
        || lifecycle.repo_id != proof.repo_id
        || lifecycle.checkout != proof.checkout
    {
        return Err(WriterError::Verify);
    }
    if clean(repo, &proof.checkout, &proof.head_at_start).is_err() {
        reset(repo, &proof.checkout, &proof.head_at_start)?;
        return Err(WriterError::Verify);
    }
    Ok(WorkspaceRecord {
        repo_id: proof.repo_id.clone(),
        toplevel: proof.checkout.to_string_lossy().into(),
        branch: None,
        base_sha: proof.base.clone(),
        head_at_start: proof.head_at_start.clone(),
        head_at_end: proof.head_at_start.clone(),
    })
}
#[cfg(test)]
#[path = "seat_pool_tests.rs"]
pub(crate) mod tests;
