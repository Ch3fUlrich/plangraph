//! Receiver-side RESULT verification. No lane updates, fetches, or publication.
use crate::{
    hosts::RepoEntry,
    writer_result::oid,
    writer_secret::{command, findings, message, object},
};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

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

/// Expected immutable bundle identity.
pub struct ExpectedBundle<'a> {
    pub sha256: &'a str,
    pub base: &'a str,
    pub head: &'a str,
}

/// Scratch-owned artifacts; dropping this value removes the verified pack and index.
#[derive(Debug)]
pub struct VerifiedBundle {
    pub head: String,
    pub base: String,
    pub pack: PathBuf,
    pub idx: PathBuf,
    _scratch: tempfile::TempDir,
}

/// Verify without writing any receiver objects, refs, or metadata.
pub fn verify_bundle(
    repo: &RepoEntry,
    bundle_path: &Path,
    expected: &ExpectedBundle<'_>,
) -> Result<VerifiedBundle, Refusal> {
    let bytes = std::fs::read(bundle_path).map_err(|_| Refusal::BadBundle)?;
    let ExpectedBundle {
        sha256,
        base: base_sha,
        head: head_sha,
    } = *expected;
    if format!("{:x}", Sha256::digest(&bytes)) != sha256 {
        return Err(Refusal::HashMismatch);
    }
    if bytes.len() as u64 > crate::writer_blob::MAX_BYTES || !oid(base_sha) || !oid(head_sha) {
        return Err(Refusal::BadBundle);
    }
    let root = Path::new(&repo.root);
    let scratch = tempfile::tempdir().map_err(|_| Refusal::BadBundle)?;
    let run = |root: &Path, args: &[&str], input: &[u8]| {
        command(root, args, input, None).map_err(|_| Refusal::BadBundle)
    };
    let objects = run(root, &["rev-parse", "--git-path", "objects"], &[])?;
    let objects = root.join(
        std::str::from_utf8(&objects)
            .map_err(|_| Refusal::BadBundle)?
            .trim(),
    );
    let objects = objects.canonicalize().map_err(|_| Refusal::BadBundle)?;
    init_scratch(root, scratch.path())?;
    std::fs::write(
        scratch.path().join("objects/info/alternates"),
        objects.to_str().ok_or(Refusal::BadBundle)?,
    )
    .map_err(|_| Refusal::BadBundle)?;
    let root = scratch.path();
    let bundle = root.join("input.bundle");
    std::fs::write(&bundle, bytes).map_err(|_| Refusal::BadBundle)?;
    let path = bundle.to_str().ok_or(Refusal::BadBundle)?;
    let git = |args: &[&str]| command(root, args, &[], None).map_err(|_| Refusal::BadBundle);
    git(&["bundle", "verify", path])?;
    let heads = git(&["bundle", "list-heads", path])?;
    // A RESULT exports exactly one named tip; never trust arbitrary refspecs.
    let heads = std::str::from_utf8(&heads).map_err(|_| Refusal::BadBundle)?;
    let lines: Vec<_> = heads.lines().collect();
    let fields: Vec<_> = heads.split_whitespace().collect();
    if lines.len() != 1
        || fields.len() != 2
        || fields[0] != head_sha
        || fields[1].starts_with("refs/tags/")
    {
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
        if ![b"Writer".as_slice(), b"Plan-Item".as_slice()]
            .iter()
            .all(|key| {
                trailers.split(|b| *b == b'\n').any(|line| {
                    line.iter().position(|b| *b == b':').is_some_and(|colon| {
                        line[..colon].eq_ignore_ascii_case(key)
                            && line[colon + 1..].iter().any(|b| !b.is_ascii_whitespace())
                    })
                })
            })
        {
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
    // Include inherited history too: neither a clean tip nor a clean range
    // excuses a secret in an older reachable message or blob.
    let reachable = git(&["rev-list", "--objects", "--no-object-names", head_sha])?;
    for sha in reachable.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
        let sha = std::str::from_utf8(sha).map_err(|_| Refusal::BadBundle)?;
        let (kind, raw) = object(root, sha).map_err(|_| Refusal::BadBundle)?;
        let content = match kind.as_str() {
            "commit" => message(&raw).map_err(|_| Refusal::BadBundle)?,
            "blob" => &raw,
            _ => continue,
        };
        if crate::writer_secret::scan_bytes(content) {
            return Err(Refusal::SecretInHistory);
        }
    }
    let objects = git(&["rev-list", "--objects", "--no-object-names", &range])?;
    let pack = run(root, &["pack-objects", "--stdout"], &objects)?;
    let installed = run(root, &["index-pack", "--stdin"], &pack)?;
    let name = pack_name(&installed, head_sha.len())?;
    let pack = root.join(format!("objects/pack/pack-{name}.pack"));
    let idx = pack.with_extension("idx");
    Ok(VerifiedBundle {
        head: head_sha.into(),
        base: base_sha.into(),
        pack,
        idx,
        _scratch: scratch,
    })
}

fn validate_objects(root: &Path, head: &str, base: &str) -> Result<(), Refusal> {
    let git = |args: &[&str]| command(root, args, &[], None).map_err(|_| Refusal::BadBundle);
    let range = format!("{base}..{head}");
    let mut allowed = git(&["rev-list", "--objects", "--no-object-names", &range])?;
    let alternates =
        std::fs::read_to_string(root.join("objects/info/alternates")).unwrap_or_default();
    if !alternates.trim().is_empty() {
        // Receiver inventory is read-only and excludes unrelated supplied objects.
        let receiver = Path::new(alternates.trim())
            .parent()
            .ok_or(Refusal::BadBundle)?;
        allowed.extend(
            command(
                receiver,
                &[
                    "cat-file",
                    "--batch-all-objects",
                    "--batch-check=%(objectname)",
                ],
                &[],
                None,
            )
            .map_err(|_| Refusal::BadBundle)?,
        );
    }
    let allowed: std::collections::BTreeSet<_> = allowed.split(|b| *b == b'\n').collect();
    // Disable alternates only for inventory: inherited receiver objects are
    // prerequisites, not objects supplied by this bundle.
    let path = command(
        root,
        &["rev-parse", "--git-path", "objects/info/alternates"],
        &[],
        None,
    )
    .map_err(|_| Refusal::BadBundle)?;
    let path = root.join(
        std::str::from_utf8(&path)
            .map_err(|_| Refusal::BadBundle)?
            .trim(),
    );
    let saved = std::fs::read(&path).ok();
    if saved.is_some() {
        std::fs::write(&path, []).map_err(|_| Refusal::BadBundle)?;
    }
    let listing = git(&[
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname)",
    ]);
    if let Some(saved) = saved {
        std::fs::write(path, saved).map_err(|_| Refusal::BadBundle)?;
    }
    let listing = listing?;
    for sha in listing.split(|b| *b == b'\n').filter(|sha| !sha.is_empty()) {
        if !allowed.contains(sha) {
            return Err(Refusal::UnexpectedObjects);
        }
    }
    Ok(())
}

fn pack_name(installed: &[u8], width: usize) -> Result<&str, Refusal> {
    let name = std::str::from_utf8(installed)
        .map_err(|_| Refusal::BadBundle)?
        .split_whitespace()
        .last()
        .ok_or(Refusal::BadBundle)?;
    if name.len() != width || !name.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(Refusal::BadBundle);
    }
    Ok(name)
}

fn init_scratch(receiver: &Path, scratch: &Path) -> Result<(), Refusal> {
    let format =
        command(receiver, &["rev-parse", "--show-object-format"], &[], None).unwrap_or_default();
    let format = if format == b"sha256\n" {
        "sha256"
    } else {
        "sha1"
    };
    if command(
        scratch,
        &["init", "--bare", &format!("--object-format={format}")],
        &[],
        None,
    )
    .is_err()
    {
        if format != "sha1" {
            return Err(Refusal::BadBundle);
        }
        command(scratch, &["init", "--bare"], &[], None).map_err(|_| Refusal::BadBundle)?;
    }
    Ok(())
}

#[cfg(test)]
#[path = "finalize_tests.rs"]
mod tests;
