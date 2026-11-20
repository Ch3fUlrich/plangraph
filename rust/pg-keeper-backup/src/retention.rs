//! Retention: keep the newest N dumps, delete the rest. Only names that fully match
//! the dump pattern are ever considered; nothing else in the directory is touched.

use crate::names;
use std::fs;
use std::io;
use std::path::Path;

/// Names to delete: the dump names beyond the newest `keep`, oldest first.
/// Newest = latest stamp, then highest seq.
pub fn plan(names_in_dir: &[String], keep: usize) -> Vec<String> {
    let mut dumps: Vec<(&str, u64, &String)> = names_in_dir
        .iter()
        .filter_map(|n| names::parse(n).map(|(seq, stamp)| (stamp, seq, n)))
        .collect();
    dumps.sort();
    let drop = dumps.len().saturating_sub(keep);
    dumps
        .into_iter()
        .take(drop)
        .map(|(_, _, n)| n.clone())
        .collect()
}

/// Delete the planned files from `dir`; returns the deleted names.
pub fn prune(dir: &Path, keep: usize) -> io::Result<Vec<String>> {
    let mut found = Vec::new();
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        if entry.file_type()?.is_file() {
            if let Some(name) = entry.file_name().to_str() {
                found.push(name.to_owned());
            }
        }
    }
    let doomed = plan(&found, keep);
    for name in &doomed {
        fs::remove_file(dir.join(name))?;
    }
    Ok(doomed)
}
