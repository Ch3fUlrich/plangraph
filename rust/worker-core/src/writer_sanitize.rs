//! Rebuild exported history without retaining original commit or secret blob links.
use crate::{
    hosts::RepoEntry,
    writer_result::{oid, WriterError},
    writer_secret::{command, entries, object, scan_bytes},
};
use std::path::Path;

pub fn sanitize_history(repo: &RepoEntry, base: &str, tip: &str) -> Result<String, WriterError> {
    if !oid(base) || !oid(tip) {
        return Err(WriterError::Invalid);
    }
    let root = Path::new(&repo.root);
    command(root, &["merge-base", "--is-ancestor", base, tip], &[], None)?;
    let commits = command(
        root,
        &["rev-list", "--reverse", &format!("{base}..{tip}")],
        &[],
        None,
    )?;
    let mut parent = base.to_owned();
    let temp = tempfile::tempdir()?;
    let index = temp.path().join("index");
    for commit in String::from_utf8(commits)
        .map_err(|_| WriterError::Git)?
        .lines()
    {
        command(root, &["read-tree", "--empty"], &[], Some(&index))?;
        let mut records = Vec::new();
        for (mode, blob, path) in entries(root, commit)? {
            if mode == "160000" || scan_bytes(&object(root, &blob)?.1) {
                continue;
            }
            records.extend_from_slice(format!("{mode} {blob}\t{path}\0").as_bytes());
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
        let next = command(
            root,
            &["commit-tree", tree, "-p", &parent],
            b"Sanitized writer salvage\n\nWriter: sanitized\n",
            None,
        )?;
        parent = String::from_utf8(next)
            .map_err(|_| WriterError::Git)?
            .trim()
            .into();
    }
    Ok(parent)
}
#[cfg(test)]
#[path = "writer_sanitize_tests.rs"]
mod tests;
