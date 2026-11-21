//! Retention: keep the newest N dumps, delete the rest. Only names that fully match
//! the dump pattern are ever considered; nothing else in the directory is touched.
//! Dumps that failed their restore test (`<name>.unverified`) never count toward the N;
//! the newest [`KEEP_UNVERIFIED`] stay for diagnosis.

use crate::names;
use std::fs;
use std::io;
use std::path::Path;

pub const KEEP_UNVERIFIED: usize = 3;

fn oldest_beyond(mut found: Vec<(&str, u64, &String)>, keep: usize) -> Vec<String> {
    found.sort();
    let drop = found.len().saturating_sub(keep);
    found
        .into_iter()
        .take(drop)
        .map(|(_, _, n)| n.clone())
        .collect()
}

/// Names to delete: verified dumps beyond the newest `keep` and unverified ones beyond
/// [`KEEP_UNVERIFIED`], oldest first. Newest = latest stamp, then highest seq.
pub fn plan(names_in_dir: &[String], keep: usize) -> Vec<String> {
    let verified = names_in_dir
        .iter()
        .filter_map(|n| names::parse(n).map(|(seq, stamp)| (stamp, seq, n)))
        .collect();
    let unverified = names_in_dir
        .iter()
        .filter_map(|n| {
            let (seq, stamp) = names::parse(n.strip_suffix(names::UNVERIFIED)?)?;
            Some((stamp, seq, n))
        })
        .collect();
    let mut doomed = oldest_beyond(verified, keep);
    doomed.extend(oldest_beyond(unverified, KEEP_UNVERIFIED));
    doomed
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
