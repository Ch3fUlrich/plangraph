//! Shared reachable-content secret policy. Never inspect serialized bundles.
use crate::{
    hosts::RepoEntry,
    workspace_resolver::git_command,
    writer_result::{oid, ContentGate, WriterError},
};
use std::{
    io::{Seek, SeekFrom, Write},
    path::Path,
    process::Stdio,
};
pub struct SecretGate;
#[derive(Debug, PartialEq, Eq)]
pub struct SecretFinding {
    pub commit: String,
    pub path: Option<String>,
}
pub fn scan_bytes(bytes: &[u8]) -> bool {
    let text: String = bytes.iter().map(|b| char::from(*b)).collect();
    crate::redact::redact(&text) != text
}
pub(crate) fn command(
    root: &Path,
    args: &[&str],
    input: &[u8],
    index: Option<&Path>,
) -> Result<Vec<u8>, WriterError> {
    let mut file = tempfile::tempfile()?;
    file.write_all(input)?;
    file.seek(SeekFrom::Start(0))?;
    let mut cmd = git_command(root);
    cmd.args(["--no-replace-objects", "-c", "core.hooksPath="])
        .args(args)
        .env("GIT_AUTHOR_NAME", "Secret gate")
        .env("GIT_AUTHOR_EMAIL", "gate@example.invalid")
        .env("GIT_COMMITTER_NAME", "Secret gate")
        .env("GIT_COMMITTER_EMAIL", "gate@example.invalid")
        .env("GIT_AUTHOR_DATE", "2000-01-01T00:00:00Z")
        .env("GIT_COMMITTER_DATE", "2000-01-01T00:00:00Z")
        .stdin(file)
        .stderr(Stdio::null());
    if let Some(index) = index {
        cmd.env("GIT_INDEX_FILE", index);
    }
    let out = cmd.output()?;
    if !out.status.success() {
        return Err(WriterError::Git);
    }
    Ok(out.stdout)
}
pub(crate) fn object(root: &Path, sha: &str) -> Result<(String, Vec<u8>), WriterError> {
    if !oid(sha) {
        return Err(WriterError::Invalid);
    }
    let bytes = command(
        root,
        &["cat-file", "--batch"],
        format!("{sha}\n").as_bytes(),
        None,
    )?;
    let end = bytes
        .iter()
        .position(|b| *b == b'\n')
        .ok_or(WriterError::Git)?;
    let header = std::str::from_utf8(&bytes[..end]).map_err(|_| WriterError::Git)?;
    let fields: Vec<_> = header.split_whitespace().collect();
    if fields.len() != 3 || fields[0] != sha {
        return Err(WriterError::Git);
    }
    let size: usize = fields[2].parse().map_err(|_| WriterError::Git)?;
    if bytes.len().checked_sub(end + 2) != Some(size) {
        return Err(WriterError::Git);
    }
    Ok((fields[1].into(), bytes[end + 1..end + 1 + size].to_vec()))
}
pub(crate) fn message(commit: &[u8]) -> Result<&[u8], WriterError> {
    let end = commit
        .windows(2)
        .position(|w| w == b"\n\n")
        .ok_or(WriterError::Git)?;
    Ok(&commit[end + 2..])
}
pub fn findings(
    repo: &RepoEntry,
    prerequisite: &str,
    tip: &str,
) -> Result<Vec<SecretFinding>, WriterError> {
    if !oid(prerequisite) || !oid(tip) {
        return Err(WriterError::Invalid);
    }
    let root = Path::new(&repo.root);
    let list = command(
        root,
        &["rev-list", "--objects", &format!("{prerequisite}..{tip}")],
        &[],
        None,
    )?;
    let mut found = Vec::new();
    for line in list.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
        let split = line.iter().position(|b| *b == b' ').unwrap_or(line.len());
        let sha = std::str::from_utf8(&line[..split]).map_err(|_| WriterError::Git)?;
        let (kind, bytes) = object(root, sha)?;
        let matching = match kind.as_str() {
            "blob" => scan_bytes(&bytes),
            "commit" => scan_bytes(message(&bytes)?),
            _ => false,
        };
        if matching {
            found.push(SecretFinding {
                commit: sha.into(),
                path: if kind == "blob" {
                    Some(
                        String::from_utf8(line.get(split + 1..).unwrap_or_default().to_vec())
                            .map_err(|_| WriterError::Verify)?,
                    )
                } else {
                    None
                },
            });
        }
    }
    Ok(found)
}
impl ContentGate for SecretGate {
    fn check(&self, repo: &RepoEntry, prerequisite: &str, tip: &str) -> Result<(), WriterError> {
        if findings(repo, prerequisite, tip)?.is_empty() {
            Ok(())
        } else {
            Err(WriterError::SecretInHistory)
        }
    }
}
#[cfg(test)]
#[path = "writer_secret_tests.rs"]
mod tests;
