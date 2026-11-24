//! A train part needs a counted review ACCEPT whose `HEAD:` line is the part head (rule 3.63 (1)).
//!
//! The ACCEPT files are the reviewer's answers in one directory. A writer status file never counts:
//! it has no `FILES READ:` line, and its `VERDICT:` line reports the writer, not a review.
use super::train::PartIn;
use std::path::{Path, PathBuf};

const MAX_FILE: u64 = 1 << 20;

/// What one review answer says about the head it names.
#[derive(Debug, PartialEq, Eq)]
pub struct Accept {
    pub head: String,
}

/// `Some` only for a review answer that accepts: a `FILES READ:` line, a full `HEAD:` sha, and the
/// last non-empty line `VERDICT: ACCEPT`.
pub fn parse(text: &str) -> Option<Accept> {
    let last = text.lines().rev().find(|l| !l.trim().is_empty())?;
    if last.trim() != "VERDICT: ACCEPT" || !text.lines().any(|l| l.starts_with("FILES READ:")) {
        return None;
    }
    let head = text
        .lines()
        .rev()
        .find_map(|l| l.trim().strip_prefix("HEAD:"))?
        .trim();
    let full = head.len() == 40 && head.bytes().all(|b| b.is_ascii_hexdigit());
    full.then(|| Accept {
        head: head.to_ascii_lowercase(),
    })
}

fn accepts(dir: &Path) -> Result<Vec<(PathBuf, Accept)>, String> {
    let mut found = Vec::new();
    let entries =
        std::fs::read_dir(dir).map_err(|e| format!("accept dir {}: {e}", dir.display()))?;
    for entry in entries {
        let path = entry.map_err(|e| e.to_string())?.path();
        let small = std::fs::metadata(&path)
            .map(|m| m.is_file() && m.len() <= MAX_FILE)
            .unwrap_or(false);
        if let Some(accept) = small
            .then(|| std::fs::read_to_string(&path).ok())
            .flatten()
            .and_then(|text| parse(&text))
        {
            found.push((path, accept));
        }
    }
    found.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(found)
}

/// Every part has a counted ACCEPT file at its exact head; returns one line per part for the PR body.
/// The error names the part, its head and the directory searched, and lists the ACCEPT files on other heads.
pub fn require(dir: &Path, parts: &[PartIn]) -> Result<Vec<String>, String> {
    let found = accepts(dir)?;
    let mut lines = Vec::new();
    for part in parts {
        let head = part.head.to_ascii_lowercase();
        match found.iter().find(|(_, accept)| accept.head == head) {
            Some((path, _)) => lines.push(format!(
                "accept #{} head {} file {}",
                part.pr,
                part.head,
                path.file_name().unwrap_or_default().to_string_lossy()
            )),
            None => {
                let older: Vec<String> = found
                    .iter()
                    .map(|(path, accept)| {
                        format!(
                            "{} on {}",
                            path.file_name().unwrap_or_default().to_string_lossy(),
                            accept.head
                        )
                    })
                    .collect();
                return Err(format!(
                    "accept: part #{} head {} has no ACCEPT file with HEAD {} in {} (ACCEPT files found: {})",
                    part.pr,
                    part.head,
                    part.head,
                    dir.display(),
                    if older.is_empty() { "none".into() } else { older.join(", ") }
                ));
            }
        }
    }
    Ok(lines)
}
