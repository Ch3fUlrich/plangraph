//! Receiver-side RESULT verification. No lane updates, fetches, or publication.
use crate::hosts::RepoEntry;
#[cfg(unix)]
use crate::writer_secret::{command, message};
#[cfg(unix)]
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
    #[error("unsupported")]
    Unsupported,
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
    #[cfg(not(unix))]
    {
        let _ = (repo, bundle_path, expected);
        Err(Refusal::Unsupported)
    }
    #[cfg(unix)]
    verify_unix(repo, bundle_path, expected)
}

#[cfg(unix)]
fn verify_unix(
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
    let receiver = Path::new(&repo.root);
    let format = command(receiver, &["rev-parse", "--show-object-format"], &[], None)
        .map_err(|_| Refusal::BadBundle)?;
    let width = match format.as_slice() {
        b"sha1\n" => 40,
        b"sha256\n" => 64,
        _ => return Err(Refusal::BadBundle),
    };
    if bytes.len() as u64 > crate::writer_blob::MAX_BYTES
        || !oid_any(base_sha, width)
        || !oid_any(head_sha, width)
    {
        return Err(Refusal::BadBundle);
    }
    let root = Path::new(&repo.root);
    let scratch = tempfile::tempdir().map_err(|_| Refusal::BadBundle)?;
    let run = |root: &Path, args: &[&str], input: &[u8]| {
        command(root, args, input, None).map_err(|_| Refusal::BadBundle)
    };
    let objects = run(root, &["rev-parse", "--git-common-dir"], &[])?;
    let objects = root.join(
        std::str::from_utf8(&objects)
            .map_err(|_| Refusal::BadBundle)?
            .trim(),
    );
    let objects = objects
        .join("objects")
        .canonicalize()
        .map_err(|_| Refusal::BadBundle)?;
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
        || !fields[1].starts_with("refs/heads/")
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
    git(&["cat-file", "-e", base_sha])?;
    if git(&["cat-file", "-t", head_sha])? != b"commit\n" {
        return Err(Refusal::BadBundle);
    }
    git(&["merge-base", "--is-ancestor", base_sha, head_sha])
        .map_err(|_| Refusal::NotDescendant)?;
    validate_objects(root, receiver, head_sha, base_sha)?;
    let range = format!("{base_sha}..{head_sha}");
    let commits = git(&["rev-list", &range])?;
    if commits.is_empty() {
        return Err(Refusal::BadBundle);
    }
    for commit in commits.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
        let commit = std::str::from_utf8(commit).map_err(|_| Refusal::BadBundle)?;
        let (_, raw) = object(root, commit, width)?;
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
    // Include inherited history too: neither a clean tip nor a clean range
    // excuses a secret in an older reachable message or blob.
    let reachable = git(&["rev-list", "--objects", "--no-object-names", head_sha])?;
    scan::reachable(root, &reachable)?;
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

#[cfg(unix)]
fn validate_objects(root: &Path, receiver: &Path, head: &str, base: &str) -> Result<(), Refusal> {
    let git = |args: &[&str]| command(root, args, &[], None).map_err(|_| Refusal::BadBundle);
    let range = format!("{base}..{head}");
    let mut allowed = git(&["rev-list", "--objects", "--no-object-names", &range])?;
    {
        // Query the actual receiver, including linked worktrees and alternates.
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

#[cfg(unix)]
fn pack_name(installed: &[u8], width: usize) -> Result<&str, Refusal> {
    let name = std::str::from_utf8(installed)
        .map_err(|_| Refusal::BadBundle)?
        .split_whitespace()
        .last()
        .ok_or(Refusal::BadBundle)?;
    if !oid_any(name, width) {
        return Err(Refusal::BadBundle);
    }
    Ok(name)
}

#[cfg(unix)]
fn oid_any(value: &str, width: usize) -> bool {
    matches!(width, 40 | 64)
        && value.len() == width
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[cfg(unix)]
fn object(root: &Path, sha: &str, width: usize) -> Result<(String, Vec<u8>), Refusal> {
    if !oid_any(sha, width) {
        return Err(Refusal::BadBundle);
    }
    let kind =
        command(root, &["cat-file", "-t", sha], &[], None).map_err(|_| Refusal::BadBundle)?;
    let kind = std::str::from_utf8(&kind)
        .map_err(|_| Refusal::BadBundle)?
        .trim();
    let raw = command(root, &["cat-file", kind, sha], &[], None).map_err(|_| Refusal::BadBundle)?;
    Ok((kind.into(), raw))
}

#[cfg(unix)]
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

#[cfg(all(test, unix))]
#[path = "finalize_tests.rs"]
mod tests;

#[cfg(all(test, not(unix)))]
#[test]
fn finalize_refuses_unsupported_platform() {
    let repo = RepoEntry {
        live_dir: None,
        root: String::new(),
        worktrees_root: String::new(),
        remote: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    assert_eq!(
        verify_bundle(
            &repo,
            Path::new("unused"),
            &ExpectedBundle {
                sha256: "",
                base: "",
                head: "",
            }
        )
        .unwrap_err(),
        Refusal::Unsupported
    );
}

#[cfg(unix)]
#[path = "finalize_scan.rs"]
mod scan;
