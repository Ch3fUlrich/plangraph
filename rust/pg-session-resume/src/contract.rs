//! The documented lane file (docs/SESSION-LIFECYCLE.md): the lane status file. Pure
//! parsing, no policy.

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

pub fn parse_status(lane: &str, text: &str) -> StatusFacts {
    let done_line = format!("{lane} DONE");
    let mut facts = StatusFacts::default();
    for line in text.lines().map(str::trim) {
        if line == done_line {
            facts.done = true;
        }
        // The last HEAD line decides; a malformed one clears an earlier valid one, so a status
        // that is half-rewritten never authorizes the old head.
        if let Some(sha) = line.strip_prefix("HEAD:").map(str::trim) {
            let valid = sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit());
            facts.head = valid.then(|| sha.to_ascii_lowercase());
        }
    }
    facts
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
