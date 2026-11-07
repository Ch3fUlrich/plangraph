//! Receiver-side RESULT verification. No lane updates, fetches, or publication.
use crate::{
    hosts::RepoEntry,
    writer_result::{oid, safe_id},
    writer_secret::{command, findings, message, object},
};
use sha2::{Digest, Sha256};
use std::{io::Write, path::Path};

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum Refusal {
    #[error("secret_in_history")]
    SecretInHistory,
    #[error("not_descendant")]
    NotDescendant,
    #[error("missing_trailer")]
    MissingTrailer,
    #[error("bad_bundle")]
    BadBundle,
    #[error("hash_mismatch")]
    HashMismatch,
    #[error("unexpected_objects")]
    UnexpectedObjects,
}

/// A verified immutable tip; callers must separately authorize publication.
#[derive(Debug, PartialEq, Eq)]
pub struct QuarantinedResult {
    pub reference: String,
    pub head_sha: String,
}

/// Import bytes bound to the expected digest and head, retaining only a quarantine ref.
/// Existing refs are never overwritten. Verification objects remain in scratch storage.
/// The receiver must already have the bundle's origin prerequisites locally.
pub fn import_result(
    repo: &RepoEntry,
    bytes: &[u8],
    sha256: &str,
    base_sha: &str,
    head_sha: &str,
    run_id: &str,
) -> Result<QuarantinedResult, Refusal> {
    if format!("{:x}", Sha256::digest(bytes)) != sha256 {
        return Err(Refusal::HashMismatch);
    }
    if bytes.len() as u64 > crate::writer_blob::MAX_BYTES
        || !oid(base_sha)
        || !oid(head_sha)
        || !safe_id(run_id)
    {
        return Err(Refusal::BadBundle);
    }
    let root = Path::new(&repo.root);
    let scratch = tempfile::tempdir().map_err(|_| Refusal::BadBundle)?;
    let run = |root: &Path, args: &[&str], input: &[u8]| {
        command(root, args, input, None).map_err(|_| Refusal::BadBundle)
    };
    let objects = run(
        root,
        &[
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        ],
        &[],
    )?;
    let format = run(root, &["rev-parse", "--show-object-format"], &[])?;
    let format = std::str::from_utf8(&format)
        .map_err(|_| Refusal::BadBundle)?
        .trim();
    run(
        scratch.path(),
        &["init", "--bare", &format!("--object-format={format}")],
        &[],
    )?;
    std::fs::write(scratch.path().join("objects/info/alternates"), objects)
        .map_err(|_| Refusal::BadBundle)?;
    let receiver = root;
    let root = scratch.path();
    let mut bundle = tempfile::NamedTempFile::new().map_err(|_| Refusal::BadBundle)?;
    bundle.write_all(bytes).map_err(|_| Refusal::BadBundle)?;
    let path = bundle.path().to_str().ok_or(Refusal::BadBundle)?;
    let git = |args: &[&str]| command(root, args, &[], None).map_err(|_| Refusal::BadBundle);
    git(&["bundle", "verify", path])?;
    let heads = git(&["bundle", "list-heads", path])?;
    // A RESULT exports exactly one named tip; never trust arbitrary refspecs.
    let heads = std::str::from_utf8(&heads).map_err(|_| Refusal::BadBundle)?;
    let lines: Vec<_> = heads.lines().collect();
    if lines.len() != 1 || lines[0].split_whitespace().next() != Some(head_sha) {
        return Err(Refusal::BadBundle);
    }
    // Unbundle installs objects only: no FETCH_HEAD, tags, branches or remote refs.
    git(&[
        "-c",
        "transfer.fsckObjects=true",
        "bundle",
        "unbundle",
        path,
    ])?;
    validate_objects(root, head_sha, base_sha)?;
    if git(&["cat-file", "-t", head_sha])? != b"commit\n" {
        return Err(Refusal::BadBundle);
    }
    git(&["merge-base", "--is-ancestor", base_sha, head_sha])
        .map_err(|_| Refusal::NotDescendant)?;
    let range = format!("{base_sha}..{head_sha}");
    let commits = git(&["rev-list", &range])?;
    if commits.is_empty() {
        return Err(Refusal::BadBundle);
    }
    for commit in commits.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
        let commit = std::str::from_utf8(commit).map_err(|_| Refusal::BadBundle)?;
        let (_, raw) = object(root, commit).map_err(|_| Refusal::BadBundle)?;
        let trailers = command(
            root,
            &["interpret-trailers", "--parse"],
            message(&raw).map_err(|_| Refusal::BadBundle)?,
            None,
        )
        .map_err(|_| Refusal::BadBundle)?;
        if !trailers.split(|b| *b == b'\n').any(|line| {
            line.iter().position(|b| *b == b':').is_some_and(|colon| {
                line[..colon].eq_ignore_ascii_case(b"Writer")
                    && line[colon + 1..].iter().any(|b| !b.is_ascii_whitespace())
            })
        }) {
            return Err(Refusal::MissingTrailer);
        }
    }
    let mut scratch_repo = repo.clone();
    scratch_repo.root = root.to_str().ok_or(Refusal::BadBundle)?.into();
    if !findings(&scratch_repo, base_sha, head_sha)
        .map_err(|_| Refusal::BadBundle)?
        .is_empty()
    {
        return Err(Refusal::SecretInHistory);
    }
    let reference = format!("refs/spawner/quarantine/{run_id}");
    let objects = git(&["rev-list", "--objects", "--no-object-names", &range])?;
    let pack = run(root, &["pack-objects", "--stdout"], &objects)?;
    promote(receiver, &pack, &reference, head_sha)?;
    Ok(QuarantinedResult {
        reference,
        head_sha: head_sha.into(),
    })
}

fn promote(root: &Path, pack: &[u8], reference: &str, head: &str) -> Result<(), Refusal> {
    let git = |args: &[&str], input: &[u8]| {
        command(root, args, input, None).map_err(|_| Refusal::BadBundle)
    };
    let objects = git(
        &[
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects/pack",
        ],
        &[],
    )?;
    let directory = Path::new(
        std::str::from_utf8(&objects)
            .map_err(|_| Refusal::BadBundle)?
            .trim(),
    );
    // Do not remove a pack that was already present before this import.
    let existing: std::collections::BTreeSet<_> = std::fs::read_dir(directory)
        .map_err(|_| Refusal::BadBundle)?
        .map(|entry| entry.map(|e| e.path()))
        .collect::<Result<_, _>>()
        .map_err(|_| Refusal::BadBundle)?;
    let installed = git(&["index-pack", "--stdin"], pack)?;
    let name = std::str::from_utf8(&installed)
        .map_err(|_| Refusal::BadBundle)?
        .split_whitespace()
        .last()
        .ok_or(Refusal::BadBundle)?;
    if name.len() != head.len() || !name.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(Refusal::BadBundle);
    }
    if git(
        &["update-ref", reference, head, &"0".repeat(head.len())],
        &[],
    )
    .is_err()
    {
        for extension in ["idx", "pack"] {
            let path = directory.join(format!("pack-{name}.{extension}"));
            if !existing.contains(&path) {
                std::fs::remove_file(path).map_err(|_| Refusal::BadBundle)?;
            }
        }
        return Err(Refusal::BadBundle);
    }
    Ok(())
}

fn validate_objects(root: &Path, head: &str, base: &str) -> Result<(), Refusal> {
    let git = |args: &[&str]| command(root, args, &[], None).map_err(|_| Refusal::BadBundle);
    let allowed = git(&["rev-list", "--objects", "--no-object-names", head, base])?;
    let allowed: std::collections::BTreeSet<_> = allowed.split(|b| *b == b'\n').collect();
    let listing = git(&[
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname)",
    ])?;
    for sha in listing.split(|b| *b == b'\n').filter(|sha| !sha.is_empty()) {
        if !allowed.contains(sha) {
            return Err(Refusal::UnexpectedObjects);
        }
    }
    Ok(())
}

#[cfg(test)]
#[path = "finalize_tests.rs"]
mod tests;
