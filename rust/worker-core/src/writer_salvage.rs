//! Sanitized salvage using a private index; never modifies the owned checkout.
use crate::{
    hosts::RepoEntry,
    writer_result::{oid, safe_id, ContentGate, WriterError},
    writer_sanitize::sanitize_history,
    writer_secret::{command, SecretGate},
};
use std::path::{Component, Path};

pub fn export_salvage(
    repo: &RepoEntry,
    base: &str,
    tip: &str,
    run: &str,
    paths: &[u8],
) -> Result<Vec<u8>, WriterError> {
    if !oid(base) || !oid(tip) || !safe_id(run) || (!paths.is_empty() && !paths.ends_with(&[0])) {
        return Err(WriterError::Invalid);
    }
    for path in paths.split(|b| *b == 0).filter(|p| !p.is_empty()) {
        let path = std::str::from_utf8(path).map_err(|_| WriterError::Invalid)?;
        if path.starts_with('-')
            || path.contains('\\')
            || !Path::new(path)
                .components()
                .all(|c| matches!(c, Component::Normal(_)))
        {
            return Err(WriterError::Invalid);
        }
    }
    let clean = sanitize_history(repo, base, tip)?;
    SecretGate.check(repo, base, &clean)?;
    let root = Path::new(&repo.root);
    let temp = tempfile::tempdir()?;
    let bundle = temp.path().join("salvage.bundle");
    // A private ref is needed because bundle creation ignores raw positive OIDs.
    let reference = format!("refs/plangraph-salvage/{run}");
    command(
        root,
        &["update-ref", &reference, &clean, &"0".repeat(40)],
        &[],
        None,
    )?;
    let result = command(
        root,
        &[
            "bundle",
            "create",
            bundle.to_str().ok_or(WriterError::Invalid)?,
            &reference,
            &format!("^{base}"),
        ],
        &[],
        None,
    );
    let cleanup = command(root, &["update-ref", "-d", &reference, &clean], &[], None);
    result?;
    cleanup?;
    let bytes = std::fs::read(bundle)?;
    if bytes.len() as u64 > crate::writer_blob::MAX_BYTES {
        return Err(WriterError::BundleTooLarge);
    }
    Ok(bytes)
}
#[cfg(test)]
#[path = "writer_salvage_tests.rs"]
mod tests;
