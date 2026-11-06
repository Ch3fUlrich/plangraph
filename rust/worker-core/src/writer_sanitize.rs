//! Rebuild exported history without retaining original commit or secret blob links.
use crate::{
    hosts::RepoEntry,
    writer_result::{oid, ContentGate, WriterError},
    writer_secret::{command, entries, message, object, scan_bytes, SecretGate},
};
use std::collections::BTreeMap;
use std::path::Path;

pub struct SanitizedHistory {
    pub tip: String,
    pub sha_map: BTreeMap<String, String>,
    pub dropped_paths: Vec<String>,
}
pub fn sanitize_history(
    repo: &RepoEntry,
    base: &str,
    tip: &str,
) -> Result<SanitizedHistory, WriterError> {
    if !oid(base) || !oid(tip) {
        return Err(WriterError::Invalid);
    }
    let root = Path::new(&repo.root);
    command(root, &["merge-base", "--is-ancestor", base, tip], &[], None)?;
    // Check every prerequisite ancestor, including root commits, through the shared gate.
    let empty_tree = command(root, &["mktree"], &[], None)?;
    let empty_tree = std::str::from_utf8(&empty_tree)
        .map_err(|_| WriterError::Git)?
        .trim();
    SecretGate.check(repo, empty_tree, base)?;
    let commits = command(
        root,
        &[
            "rev-list",
            "--reverse",
            "--topo-order",
            &format!("{base}..{tip}"),
        ],
        &[],
        None,
    )?;
    let mut result = SanitizedHistory {
        tip: base.into(),
        sha_map: BTreeMap::new(),
        dropped_paths: Vec::new(),
    };
    let temp = tempfile::tempdir()?;
    let index = temp.path().join("index");
    for commit in String::from_utf8(commits)
        .map_err(|_| WriterError::Git)?
        .lines()
    {
        command(root, &["read-tree", "--empty"], &[], Some(&index))?;
        let mut records = Vec::new();
        for (mode, blob, path) in entries(root, commit)? {
            let path_secret = scan_bytes(path.as_bytes());
            if path_secret || (mode != "160000" && scan_bytes(&object(root, &blob)?.1)) {
                result.dropped_paths.push(if path_secret {
                    "<path removed by secret gate>".into()
                } else {
                    path
                });
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
        let raw = object(root, commit)?.1;
        let mut args = vec!["commit-tree".to_owned(), tree.to_owned()];
        for line in raw.split(|b| *b == b'\n').take_while(|l| !l.is_empty()) {
            if let Some(parent) = line.strip_prefix(b"parent ") {
                let parent = std::str::from_utf8(parent).map_err(|_| WriterError::Git)?;
                args.extend([
                    "-p".into(),
                    result.sha_map.get(parent).cloned().unwrap_or(parent.into()),
                ]);
            }
        }
        let original = message(&raw)?;
        let mut msg = if scan_bytes(original) {
            b"Message removed by secret gate\n".to_vec()
        } else {
            original.to_vec()
        };
        msg.extend_from_slice(b"\n\nSanitized: secret-gate\n");
        let next = command(
            root,
            &args.iter().map(String::as_str).collect::<Vec<_>>(),
            &msg,
            None,
        )?;
        result.tip = String::from_utf8(next)
            .map_err(|_| WriterError::Git)?
            .trim()
            .into();
        result.sha_map.insert(commit.into(), result.tip.clone());
    }
    result.dropped_paths.sort();
    result.dropped_paths.dedup();
    #[cfg(test)]
    REBUILD_LEAK.with(|leak| {
        if leak.get() {
            result.tip = tip.into();
        }
    });
    SecretGate.check(repo, base, &result.tip)?;
    Ok(result)
}
#[cfg(test)]
#[path = "writer_sanitize_tests.rs"]
mod tests;

#[cfg(test)]
std::thread_local! {
    static REBUILD_LEAK: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}
