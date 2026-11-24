//! The documented lane files (docs/SESSION-LIFECYCLE.md): a lane status file and
//! numbered findings files in one directory. Pure parsing, no policy.

use std::path::{Path, PathBuf};

/// What the status file of one lane says about the writer.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct StatusFacts {
    /// The line `<lane> DONE` is present.
    pub done: bool,
    /// The last `HEAD: <40 hex>` line.
    pub head: Option<String>,
}

pub fn status_path(dir: &Path, lane: &str) -> PathBuf {
    dir.join(format!("{lane}.status"))
}

pub fn findings_name(lane: &str, round: u64) -> String {
    format!("{lane}.fix-round-{round}.txt")
}

pub fn parse_status(lane: &str, text: &str) -> StatusFacts {
    let done_line = format!("{lane} DONE");
    let mut facts = StatusFacts::default();
    for line in text.lines().map(str::trim) {
        if line == done_line {
            facts.done = true;
        }
        if let Some(sha) = line.strip_prefix("HEAD:").map(str::trim) {
            if sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()) {
                facts.head = Some(sha.to_ascii_lowercase());
            }
        }
    }
    facts
}

/// The round number of `<lane>.fix-round-<N>.txt`, None for any other name.
pub fn findings_round(lane: &str, file_name: &str) -> Option<u64> {
    let digits = file_name
        .strip_prefix(lane)?
        .strip_prefix(".fix-round-")?
        .strip_suffix(".txt")?;
    if digits.is_empty() || digits.len() > 9 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    digits.parse().ok().filter(|n| *n > 0)
}

/// Every findings file of the lane, ascending by round.
pub fn list_findings(dir: &Path, lane: &str) -> Vec<(u64, PathBuf)> {
    let mut found: Vec<(u64, PathBuf)> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let round = findings_round(lane, e.file_name().to_str()?)?;
            Some((round, e.path()))
        })
        .collect();
    found.sort();
    found
}

pub fn highest_round(dir: &Path, lane: &str) -> u64 {
    list_findings(dir, lane).last().map_or(0, |(n, _)| *n)
}

/// `*` matches any run of characters; everything else is literal.
pub fn glob_match(pattern: &str, text: &str) -> bool {
    let parts: Vec<&str> = pattern.split('*').collect();
    let (first, last) = (parts[0], parts[parts.len() - 1]);
    if parts.len() == 1 {
        return pattern == text;
    }
    if !text.starts_with(first) || text.len() < first.len() + last.len() || !text.ends_with(last) {
        return false;
    }
    let mut rest = &text[first.len()..text.len() - last.len()];
    for mid in &parts[1..parts.len() - 1] {
        match rest.find(mid) {
            Some(i) => rest = &rest[i + mid.len()..],
            None => return false,
        }
    }
    true
}
