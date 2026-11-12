//! Post-run verification and sealed export; never publication or worktree removal.
use crate::writer_blob::{BlobStore, SealedBlob};
use crate::{
    hosts::RepoEntry,
    workspace::{Mode, Workspace},
    workspace_resolver::git_command,
};
use std::{
    io::Write,
    path::{Path, PathBuf},
    process::Stdio,
};

#[derive(Debug, thiserror::Error)]
pub enum WriterError {
    #[error("invalid")]
    Invalid,
    #[error("verify")]
    Verify,
    #[error("git")]
    Git,
    #[error("salvage cleanup failed: {cleanup} (bundle error: {bundle:?})")]
    SalvageCleanup {
        bundle: Option<Box<WriterError>>,
        #[source]
        cleanup: Box<WriterError>,
    },
    #[error("io")]
    Io,
    #[error("secret_in_history")]
    SecretInHistory,
    #[error("bundle_too_large")]
    BundleTooLarge,
    #[error("unrecorded")]
    Unrecorded,
    #[error("conflict")]
    Conflict,
    #[error("HOLD gate file-size: {0}")]
    FileSize(String),
    #[error("HOLD gate changelog: {0}")]
    Changelog(String),
}
impl From<std::io::Error> for WriterError {
    fn from(_: std::io::Error) -> Self {
        Self::Io
    }
}
pub(crate) fn safe_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 80
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
}
pub(crate) fn oid(s: &str) -> bool {
    s.len() == 40
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn git(path: &Path, args: &[&str]) -> Result<String, WriterError> {
    let out = git_command(path)
        .args(["-c", "core.hooksPath="])
        .args(args)
        .output()?;
    if !out.status.success() {
        return Err(WriterError::Git);
    }
    Ok(String::from_utf8(out.stdout)
        .map_err(|_| WriterError::Git)?
        .trim_end_matches(['\r', '\n'])
        .into())
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckedResult {
    pub repo_id: String,
    pub base_sha: String,
    pub head_sha: String,
    pub branch: String,
    pub commits: Vec<String>,
    checkout: PathBuf,
}
pub fn check_result(
    repo: &RepoEntry,
    checkout: &Path,
    workspace: &Workspace,
) -> Result<CheckedResult, WriterError> {
    let base = workspace
        .base_sha
        .as_deref()
        .filter(|base| !base.is_empty())
        .ok_or_else(|| WriterError::FileSize("no base".into()))?;
    if !oid(base) {
        return Err(WriterError::Invalid);
    }
    workspace.validate().map_err(|_| WriterError::Invalid)?;
    if workspace.mode != Mode::Writer {
        return Err(WriterError::Invalid);
    }
    crate::workspace_launch::verify_owned(repo, checkout).map_err(|_| WriterError::Verify)?;
    let branch = workspace.branch.as_deref().ok_or(WriterError::Invalid)?;
    let head = git(checkout, &["rev-parse", "HEAD"])?;
    if !oid(&head)
        || git(checkout, &["symbolic-ref", "HEAD"])? != format!("refs/heads/{branch}")
        || !git(
            checkout,
            &["status", "--porcelain=v1", "--untracked-files=all"],
        )?
        .is_empty()
    {
        return Err(WriterError::Verify);
    }
    git(checkout, &["merge-base", "--is-ancestor", base, &head])?;
    let commits: Vec<String> = git(checkout, &["rev-list", &format!("{base}..{head}")])?
        .lines()
        .map(str::to_owned)
        .collect();
    if commits.is_empty() {
        return Err(WriterError::Verify);
    }
    for commit in &commits {
        if !oid(commit) {
            return Err(WriterError::Verify);
        }
        let message = git(checkout, &["show", "-s", "--format=%B", commit])?;
        let mut child = git_command(checkout)
            .args(["interpret-trailers", "--parse"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        child
            .stdin
            .take()
            .ok_or(WriterError::Io)?
            .write_all(message.as_bytes())?;
        let out = child.wait_with_output()?;
        if !out.status.success()
            || !String::from_utf8_lossy(&out.stdout).lines().any(|line| {
                line.split_once(':').is_some_and(|(key, value)| {
                    key.eq_ignore_ascii_case("Writer") && !value.trim().is_empty()
                })
            })
        {
            return Err(WriterError::Verify);
        }
    }
    file_size::check(checkout, base, &head).map_err(WriterError::FileSize)?;
    changelog::check(checkout, base, &head).map_err(WriterError::Changelog)?;
    Ok(CheckedResult {
        repo_id: workspace.repo_id.clone(),
        base_sha: base.into(),
        head_sha: head,
        branch: branch.into(),
        commits,
        checkout: checkout.canonicalize()?,
    })
}
pub trait ContentGate {
    fn check(&self, repo: &RepoEntry, prerequisite: &str, tip: &str) -> Result<(), WriterError>;
}
pub fn export_result(
    repo: &RepoEntry,
    checked: &CheckedResult,
    run_id: &str,
    gate: &dyn ContentGate,
    blobs: &mut dyn BlobStore,
) -> Result<SealedBlob, WriterError> {
    if !safe_id(run_id) {
        return Err(WriterError::Invalid);
    }
    let workspace = Workspace {
        repo_id: checked.repo_id.clone(),
        mode: Mode::Writer,
        base_sha: Some(checked.base_sha.clone()),
        revision_sha: Some(checked.base_sha.clone()),
        branch: Some(checked.branch.clone()),
    };
    if check_result(repo, &checked.checkout, &workspace)? != *checked {
        return Err(WriterError::Verify);
    }
    let root = Path::new(&repo.root);
    let default = git(root, &["symbolic-ref", "refs/remotes/origin/HEAD"])?;
    if !default.starts_with("refs/remotes/origin/") || default == "refs/remotes/origin/HEAD" {
        return Err(WriterError::Invalid);
    }
    let prerequisite = git(root, &["merge-base", &checked.head_sha, &default])?;
    if !oid(&prerequisite) {
        return Err(WriterError::Invalid);
    }
    gate.check(repo, &prerequisite, &checked.head_sha)?;
    let dir = tempfile::tempdir()?;
    let output = dir.path().join("result.bundle");
    // R4 may fetch a newer default. Its prerequisite must remain inside the gated range.
    crate::workspace_bundle::export_bundle(repo, &checked.head_sha, run_id, &output)
        .map_err(|_| WriterError::Git)?;
    if std::fs::metadata(&output)?.len() > crate::writer_blob::MAX_BYTES {
        return Err(WriterError::BundleTooLarge);
    }
    let bytes = std::fs::read(output)?;
    let header = bytes
        .split(|b| *b == b'\n')
        .take_while(|line| !line.is_empty());
    let prerequisites: Vec<_> = header.filter(|line| line.starts_with(b"-")).collect();
    if prerequisites.len() != 1
        || !prerequisites[0].starts_with(format!("-{prerequisite} ").as_bytes())
    {
        return Err(WriterError::Verify);
    }
    blobs.seal(&bytes)
}

#[cfg(test)]
#[path = "writer_result_tests.rs"]
mod tests;

#[path = "writer_file_size.rs"]
mod file_size;

#[path = "writer_changelog.rs"]
mod changelog;
