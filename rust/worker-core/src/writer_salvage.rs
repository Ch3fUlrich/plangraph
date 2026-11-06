//! Sanitized salvage using a private index; never modifies the owned checkout.
use crate::{
    hosts::RepoEntry,
    workspace::{Mode, Workspace},
    writer_blob::{BlobStore, SealedBlob},
    writer_result::{oid, safe_id, ContentGate, WriterError},
    writer_sanitize::sanitize_history_with_drops,
    writer_secret::{command, entries, scan_bytes, SecretGate},
};
use std::collections::BTreeMap;
use std::path::{Component, Path};

pub struct SalvageResult {
    pub blob: SealedBlob,
    pub head_sha: String,
    pub sha_map: BTreeMap<String, String>,
    pub dropped_paths: Vec<String>,
}
pub fn export_salvage(
    repo: &RepoEntry,
    checkout: &Path,
    workspace: &Workspace,
    run: &str,
    blobs: &mut dyn BlobStore,
) -> Result<SalvageResult, WriterError> {
    workspace.validate().map_err(|_| WriterError::Invalid)?;
    if workspace.mode != Mode::Writer {
        return Err(WriterError::Invalid);
    }
    let base = workspace.base_sha.as_deref().ok_or(WriterError::Invalid)?;
    if !oid(base) || !safe_id(run) {
        return Err(WriterError::Invalid);
    }
    crate::workspace_launch::verify_owned(repo, checkout).map_err(|_| WriterError::Verify)?;
    let head = command(checkout, &["rev-parse", "HEAD"], &[], None)?;
    let tip = std::str::from_utf8(&head)
        .map_err(|_| WriterError::Git)?
        .trim();
    if !oid(tip) {
        return Err(WriterError::Invalid);
    }
    command(
        checkout,
        &["merge-base", "--is-ancestor", base, tip],
        &[],
        None,
    )
    .map_err(|_| WriterError::Verify)?;
    let local = RepoEntry {
        root: checkout.to_str().ok_or(WriterError::Invalid)?.into(),
        ..repo.clone()
    };
    let (snapshot, dropped) = snapshot(checkout, base, tip)?;
    let clean = sanitize_history_with_drops(&local, base, &snapshot, dropped)?;

    SecretGate.check(&local, base, &clean.tip)?;
    let root = checkout;
    let temp = tempfile::tempdir()?;
    let bundle = temp.path().join("salvage.bundle");
    let bundle_path = bundle.to_str().ok_or(WriterError::Invalid)?;
    let reference = format!("refs/plangraph-salvage/{run}");
    command(
        root,
        &["update-ref", &reference, &clean.tip, &"0".repeat(40)],
        &[],
        None,
    )?;
    let result = command(
        root,
        &[
            "bundle",
            "create",
            bundle_path,
            &reference,
            &format!("^{base}"),
        ],
        &[],
        None,
    );
    finish_bundle(result, || {
        command(
            root,
            &["update-ref", "-d", &reference, &clean.tip],
            &[],
            None,
        )
    })?;
    if std::fs::metadata(&bundle)?.len() > crate::writer_blob::MAX_BYTES {
        return Err(WriterError::BundleTooLarge);
    }
    let bytes = std::fs::read(bundle)?;
    Ok(SalvageResult {
        blob: blobs.seal(&bytes)?,
        head_sha: clean.tip,
        sha_map: clean.sha_map,
        dropped_paths: clean.dropped_paths,
    })
}
// Always run cleanup, and report its failure even when bundle creation also failed.
fn finish_bundle(
    result: Result<Vec<u8>, WriterError>,
    cleanup: impl FnOnce() -> Result<Vec<u8>, WriterError>,
) -> Result<(), WriterError> {
    if cleanup().is_err() {
        return Err(WriterError::SalvageCleanup {
            bundle_failed: result.is_err(),
        });
    }
    result.map(|_| ())
}
fn validate_paths(paths: &[u8]) -> Result<(), WriterError> {
    if !paths.is_empty() && !paths.ends_with(&[0]) {
        return Err(WriterError::Invalid);
    }
    for path in paths.split(|b| *b == 0).filter(|p| !p.is_empty()) {
        let path = std::str::from_utf8(path).map_err(|_| WriterError::Invalid)?;
        if path.starts_with('-')
            || path.contains('\\')
            || path.split('/').any(|part| part == "." || part == "..")
            || !Path::new(path)
                .components()
                .all(|c| matches!(c, Component::Normal(_)))
        {
            return Err(WriterError::Invalid);
        }
    }
    Ok(())
}
fn snapshot(root: &Path, base: &str, tip: &str) -> Result<(String, Vec<Vec<u8>>), WriterError> {
    let mut listed = command(
        root,
        &["diff", "--name-only", "-z", "--no-renames", tip, "--"],
        &[],
        None,
    )?;
    listed.extend(command(
        root,
        &["ls-files", "-z", "--others", "--exclude-standard"],
        &[],
        None,
    )?);
    validate_paths(&listed)?;
    // Preserve unchanged entries for history sanitization; scan only listed dirty paths.
    let original = entries(root, tip)?;
    let mut files = BTreeMap::new();
    let mut dropped = Vec::new();
    for name in listed.split(|b| *b == 0).filter(|p| !p.is_empty()) {
        let name = std::str::from_utf8(name).map_err(|_| WriterError::Invalid)?;
        let mut full = root.to_path_buf();
        for part in Path::new(name).components() {
            full.push(part);
            if let Ok(meta) = std::fs::symlink_metadata(&full) {
                if meta.file_type().is_symlink() {
                    return Err(WriterError::Verify);
                }
            }
        }
        match std::fs::symlink_metadata(&full) {
            Ok(meta) if meta.is_file() => {
                let bytes = std::fs::read(&full)?;
                if scan_bytes(name.as_bytes()) || scan_bytes(&bytes) {
                    dropped.push(name.as_bytes().to_vec());
                } else {
                    #[cfg(unix)]
                    let executable = {
                        use std::os::unix::fs::PermissionsExt;
                        meta.permissions().mode() & 0o111 != 0
                    };
                    #[cfg(not(unix))]
                    let executable = original
                        .iter()
                        .any(|e| e.2 == name.as_bytes() && e.0 == "100755");
                    files.insert(
                        name.to_owned(),
                        (bytes, if executable { "100755" } else { "100644" }),
                    );
                }
            }
            Ok(_) => return Err(WriterError::Verify),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    let base_entries = entries(root, base)?;
    for entry @ (mode, _, path) in &original {
        if (mode == "160000" && !base_entries.contains(entry))
            || ((mode == "160000" || mode == "120000")
                && listed.split(|b| *b == 0).any(|p| p == path))
        {
            return Err(WriterError::Verify);
        }
    }
    // Scan all bytes before creating any object; never let Git re-read mutable files.
    let temp = tempfile::tempdir()?;
    let index = temp.path().join("index");
    command(root, &["read-tree", tip], &[], Some(&index))?;
    let mut records = Vec::new();
    for name in listed.split(|b| *b == 0).filter(|p| !p.is_empty()) {
        let name = std::str::from_utf8(name).map_err(|_| WriterError::Invalid)?;
        if let Some((bytes, mode)) = files.get(name) {
            let sha = command(root, &["hash-object", "-w", "--stdin"], bytes, None)?;
            let sha = std::str::from_utf8(&sha)
                .map_err(|_| WriterError::Git)?
                .trim();
            records.extend_from_slice(format!("{mode} {sha}\t{name}\0").as_bytes());
        } else {
            records.extend_from_slice(format!("0 {}\t{name}\0", "0".repeat(40)).as_bytes());
        }
    }
    command(
        root,
        &["update-index", "-z", "--index-info"],
        &records,
        Some(&index),
    )?;
    let tree = command(root, &["write-tree"], &[], Some(&index))?;
    let tree = std::str::from_utf8(&tree)
        .map_err(|_| WriterError::Git)?
        .trim();
    let commit = command(
        root,
        &["commit-tree", tree, "-p", tip],
        b"Writer snapshot\n",
        None,
    )?;
    Ok((
        std::str::from_utf8(&commit)
            .map_err(|_| WriterError::Git)?
            .trim()
            .into(),
        dropped,
    ))
}
#[cfg(test)]
#[path = "writer_salvage_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "writer_salvage_snapshot_tests.rs"]
mod snapshot_tests;
