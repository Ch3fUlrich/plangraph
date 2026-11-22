//! Lands a batch on a router GO row: merge, MERGED line, close-merged dry run. No other trigger lands.
//!
//! A row is one line with the tokens `GO`, `D-<id>`, a batch pull request (`#<n>` or `<n>`), the full
//! 40-hex head and any number of `plangraph:<KEY>` item keys. A pass reads the row file once, skips the
//! rows the done file already holds and handles each new row exactly once: the head is unmoved, behind 0,
//! the required checks are green, then `Landing::land` merges and the result goes to routing. A moved
//! head or a red check posts `BLOCKED <reason>` and lands nothing.
use std::{collections::BTreeSet, io::Write, path::Path};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct GoRow {
    pub decision: String,
    pub pr: u64,
    pub head: String,
    pub keys: Vec<String>,
    pub text: String,
}

/// The row of one line, or `None` when the line is no GO row for a batch head.
pub(crate) fn parse_row(line: &str) -> Option<GoRow> {
    let tokens: Vec<&str> = line.split_whitespace().collect();
    if !tokens.contains(&"GO") {
        return None;
    }
    let one = |pick: &dyn Fn(&str) -> bool| {
        let mut found = tokens.iter().copied().filter(|t| pick(t));
        match (found.next(), found.next()) {
            (Some(t), None) => Some(t),
            _ => None,
        }
    };
    let head =
        one(&|t| t.len() == 40 && t.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))?;
    let decision = one(&|t| {
        t.strip_prefix("D-")
            .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
    })?;
    let pr = one(&|t| {
        let n = t.strip_prefix('#').unwrap_or(t);
        !n.is_empty() && n.len() < 10 && n.bytes().all(|b| b.is_ascii_digit())
    })?;
    Some(GoRow {
        decision: decision.into(),
        pr: pr.trim_start_matches('#').parse().ok()?,
        head: head.into(),
        keys: tokens
            .iter()
            .filter(|t| t.starts_with("plangraph:"))
            .map(|t| t.to_string())
            .collect(),
        text: line.trim().into(),
    })
}

/// Every effect of a land; the shell implementation runs `gh`, `merge_batch.py land` and keeper.
pub(crate) trait Landing {
    /// The head GitHub shows for the batch pull request now.
    fn pr_head(&self, pr: u64) -> Result<String, String>;
    /// Commits the head is behind main.
    fn behind_by(&self, head: &str) -> Result<u64, String>;
    /// `Ok` when every required check is green, else the check names and states.
    fn checks_green(&self, pr: u64) -> Result<(), String>;
    /// Runs `merge_batch land` with the row as approval; the merge commit sha.
    fn land(&self, row: &GoRow) -> Result<String, String>;
    /// One line to routing.
    fn post(&self, line: &str) -> Result<(), String>;
    /// The proposed transition from `close-merged KEY D-ID`.
    fn close_merged_dry_run(&self, key: &str, decision: &str) -> Result<String, String>;
}

/// The reason nothing lands, or `None` when the head is fit to land.
pub(crate) fn refusal(row: &GoRow, landing: &dyn Landing) -> Option<String> {
    match landing.pr_head(row.pr) {
        Ok(now) if now == row.head => {}
        Ok(now) => return Some(format!("head moved: row {} now {now}", row.head)),
        Err(e) => return Some(format!("head unreadable: {e}")),
    }
    match landing.behind_by(&row.head) {
        Ok(0) => {}
        Ok(n) => return Some(format!("head is {n} commit(s) behind main")),
        Err(e) => return Some(format!("behind main unreadable: {e}")),
    }
    landing
        .checks_green(row.pr)
        .err()
        .map(|e| format!("required checks not green: {e}"))
}

/// Handles one row; the outcome word and the lines it posted.
pub(crate) fn handle(row: &GoRow, landing: &dyn Landing) -> (String, Vec<String>) {
    let blocked = |why: String| {
        let line = format!(
            "BLOCKED land {} #{} {} | {why}",
            row.decision, row.pr, row.head
        );
        let _ = landing.post(&line);
        ("BLOCKED".to_string(), vec![line])
    };
    if let Some(why) = refusal(row, landing) {
        return blocked(why);
    }
    let merge = match landing.land(row) {
        Ok(sha) => sha,
        Err(e) => return blocked(format!("land refused: {e}")),
    };
    let mut lines = vec![format!(
        "MERGED {merge} #{} {} {}",
        row.pr, row.head, row.decision
    )];
    if row.keys.is_empty() {
        lines.push(format!(
            "close-merged dry run skipped {}: the row names no plangraph: key",
            row.decision
        ));
    }
    for key in &row.keys {
        lines.push(match landing.close_merged_dry_run(key, &row.decision) {
            Ok(result) => format!("close-merged dry run {key} {}: {result}", row.decision),
            Err(e) => format!("close-merged dry run {key} {} refused: {e}", row.decision),
        });
    }
    for line in &lines {
        let _ = landing.post(line);
    }
    ("MERGED".to_string(), lines)
}

/// One pass over the row file: the done file keeps `<D-id> <head> <outcome>` per handled row, so a row
/// is never handled twice, and a BLOCKED row is handled again only under a new head (a new row).
pub(crate) fn pass(
    rows: &Path,
    done: &Path,
    landing: &dyn Landing,
    out: &mut dyn Write,
) -> Result<usize, String> {
    let text = match std::fs::read_to_string(rows) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(format!("cannot read {}: {e}", rows.display())),
    };
    let done_text = match std::fs::read_to_string(done) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(format!("cannot read {}: {e}", done.display())),
    };
    let mut seen: BTreeSet<String> = done_text
        .lines()
        .filter_map(|l| {
            let mut t = l.split_whitespace();
            Some(format!("{} {}", t.next()?, t.next()?))
        })
        .collect();
    let mut handled = 0;
    for row in text.lines().filter_map(parse_row) {
        if !seen.insert(format!("{} {}", row.decision, row.head)) {
            continue;
        }
        let (outcome, lines) = handle(&row, landing);
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(done)
            .map_err(|e| format!("cannot write {}: {e}", done.display()))?;
        writeln!(file, "{} {} {outcome}", row.decision, row.head).map_err(|e| e.to_string())?;
        for line in lines {
            writeln!(out, "{line}").map_err(|e| e.to_string())?;
        }
        handled += 1;
    }
    Ok(handled)
}
