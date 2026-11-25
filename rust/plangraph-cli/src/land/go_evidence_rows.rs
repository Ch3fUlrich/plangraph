//! The GO and Gate-Edit rows of the decisions log, read the way the Keeper reads them.
//!
//! The text comes from the decisions document as committed on `origin/main` (never the local file), and a
//! row is a GO by the grammar of `plangraph.keeper.store_waiver` (`_go_statement`, `_go_binding`): a timestamp
//! prefix, a `RULINGS` row with one numbered GO ruling, backticked numbers and heads. A Gate-Edit grant is a
//! row the operator wrote.
use super::super::go_verify::{Approval, Grant};
use regex::Regex;
use std::path::Path;
use std::process::Command;

fn strip_prefix(text: &str) -> &str {
    let prefix = Regex::new(r"^\(~?(?:[01][0-9]|2[0-3]):[0-5][0-9x]Z(?:, date -u)?\)\s+").unwrap();
    prefix.find(text).map_or(text, |m| &text[m.end()..])
}

fn starts_go(text: &str) -> bool {
    Regex::new(r"^GO(?:\s|$)").unwrap().is_match(text)
}

/// The text of the GO verdict of a decision cell, or `None` when the row holds no single GO.
fn go_statement(cell: &str) -> Option<String> {
    let text = strip_prefix(cell);
    if starts_go(text) {
        return Some(text.to_owned());
    }
    if !Regex::new(r"^RULINGS\b").unwrap().is_match(text) {
        return None;
    }
    let mark = Regex::new(r"(?:^|\s)\((\d{1,2})\)\s+").unwrap();
    // A mark starts at its parenthesis; the whitespace before it belongs to the text before.
    let marks: Vec<(usize, usize)> = mark
        .find_iter(text)
        .map(|m| {
            let lead = m.as_str().len() - m.as_str().trim_start().len();
            (m.start() + lead, m.end())
        })
        .collect();
    let goes: Vec<usize> = (0..marks.len())
        .filter(|&i| starts_go(&text[marks[i].1..]))
        .collect();
    let [only] = goes[..] else {
        return None;
    };
    let end = marks.get(only + 1).map_or(text.len(), |m| m.0);
    Some(text[marks[only].1..end].trim().to_owned())
}

/// `(pr, head)` of a GO that names one pull request and one full `GO-HEAD`.
fn go_binding(statement: &str, project: &str) -> Option<(u64, String)> {
    let named = Regex::new(&format!(
        r"^GO\s+(?:{}\s+PR\s+(?:#|`#)?|(?:`#|#))([0-9]+)(`?)(?:$|[\s,;:.])",
        regex::escape(project)
    ))
    .unwrap()
    .captures(statement)?;
    let consumed = &statement[..named.get(0)?.end()];
    if consumed.matches('`').count() % 2 == 1 {
        return None;
    }
    // The Keeper's `(?<![\w-])GO-HEAD\s+`?(40 hex)(?![\w])`: the boundaries are looked at, not consumed.
    let head = Regex::new(r"GO-HEAD\s+`?([0-9a-fA-F]{40})").unwrap();
    let word = |c: char| c.is_alphanumeric() || c == '_';
    let heads: Vec<_> = head
        .captures_iter(statement)
        .filter(|c| {
            let whole = c.get(0).unwrap();
            let before = statement[..whole.start()].chars().next_back();
            let after = statement[whole.end()..].chars().next();
            !before.is_some_and(|b| word(b) || b == '-') && !after.is_some_and(word)
        })
        .collect();
    let [one] = &heads[..] else {
        return None;
    };
    Some((named[1].parse().ok()?, one[1].to_ascii_lowercase()))
}

/// The lines outside fenced and indented code blocks, as the Keeper reads its decisions
/// (`evidence_decisions._prose_lines`): a fence line toggles, a line of four spaces or a tab is code.
fn prose_lines(text: &str) -> Vec<&str> {
    let fence = Regex::new(r"^ {0,3}(?:`{3,}|~{3,})").unwrap();
    let mut fenced = false;
    let mut out = Vec::new();
    for line in text.lines() {
        if fence.is_match(line) {
            fenced = !fenced;
        } else if !fenced && !line.starts_with("    ") && !line.starts_with('\t') {
            out.push(line);
        }
    }
    out
}

/// The decision book of the Keeper (`evidence_decisions.decision_book`): the prose rows by decision number in
/// document order, and the decisions a later row retires (`superseded_map`). A number with more than one row,
/// or a retired one, authorizes nothing.
struct Book {
    rows: Vec<(u64, Vec<String>)>,
    retired: std::collections::BTreeSet<u64>,
}

fn decision_cell(row: &str) -> &str {
    row.split('|').nth(3).map_or("", str::trim)
}

fn book(text: &str) -> Book {
    let line = Regex::new(r"^\| D-([0-9]+) \|").unwrap();
    let mut rows: Vec<(u64, Vec<String>)> = Vec::new();
    for raw in prose_lines(text) {
        let Some(n) = line.captures(raw).and_then(|c| c[1].parse().ok()) else {
            continue;
        };
        match rows.iter_mut().find(|(id, _)| *id == n) {
            Some((_, found)) => found.push(raw.to_owned()),
            None => rows.push((n, vec![raw.to_owned()])),
        }
    }
    let own =
        Regex::new(r"(?i)^\W{0,3}(?:superseded|withdrawn|replaced|corrected)\s+by\s+D-0*(\d+)\b")
            .unwrap();
    let retires =
        Regex::new(r"(?i)\b(?:supersedes|corrects|withdraws|replaces)\s+D-0*(\d+)\b").unwrap();
    let mut out: std::collections::BTreeMap<u64, u64> = std::collections::BTreeMap::new();
    for (n, lines) in &rows {
        for row in lines {
            let cell = decision_cell(row);
            if let Some(later) = own.captures(cell).and_then(|c| c[1].parse::<u64>().ok()) {
                if later > *n {
                    out.entry(*n).or_insert(later);
                }
            }
            for found in retires.captures_iter(cell) {
                let Ok(old) = found[1].parse::<u64>() else {
                    continue;
                };
                let newest = out.get(&old).copied();
                if old < *n && newest.is_none_or(|v| v < *n) {
                    out.insert(old, *n);
                }
            }
        }
    }
    Book {
        rows,
        retired: out.into_keys().collect(),
    }
}

/// Whether the decisions log gives the operator authority on a row: the BY cell is a comma or plus separated
/// list that holds `operator`.
fn by_operator(by: &str) -> bool {
    by.split([',', '+'])
        .any(|t| t.trim().eq_ignore_ascii_case("operator"))
}

/// The approvals and Gate-Edit grants of the decisions log for this repository and pull request.
pub(in crate::land) fn rows(text: &str, repo: &str, pr: u64) -> (Vec<Approval>, Vec<Grant>) {
    let name = repo.rsplit('/').next().unwrap_or(repo);
    let grant = Regex::new(r"^GATE-EDIT\s+(\S+)\s+GO-HEAD\s+([0-9a-fA-F]{40})\s*$").unwrap();
    let book = book(text);
    let (mut approvals, mut grants) = (Vec::new(), Vec::new());
    for (n, lines) in &book.rows {
        let [row] = &lines[..] else {
            continue;
        };
        if book.retired.contains(n) {
            continue;
        }
        let cells: Vec<&str> = row.split('|').map(str::trim).collect();
        let [_, _, project, decision, by, ..] = cells[..] else {
            continue;
        };
        let d_id = format!("D-{n}");
        if project != name && project != repo {
            continue;
        }
        if let Some(c) = grant.captures(strip_prefix(decision)) {
            if by_operator(by) {
                grants.push(Grant {
                    d_id,
                    prefix: c[1].into(),
                    head: c[2].to_ascii_lowercase(),
                });
            }
            continue;
        }
        let Some(statement) = go_statement(decision) else {
            continue;
        };
        if let Some((number, head)) = [repo, name].iter().find_map(|p| go_binding(&statement, p)) {
            if number == pr {
                approvals.push(Approval {
                    d_id,
                    repo: repo.into(),
                    pr,
                    head,
                });
            }
        }
    }
    (approvals, grants)
}

fn git(dir: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let mut command = Command::new("git");
    pg_worker_core::host_command::isolate_git(&mut command);
    // Replacement refs would let a local object stand in for a committed one.
    command
        .arg("--no-replace-objects")
        .arg("-C")
        .arg(dir)
        .args(args);
    pg_worker_core::host_command::output(&mut command, std::time::Duration::from_secs(60))
        .map_err(|_| format!("git {} failed", args[0]))
}

/// The largest decisions document the collector reads.
const MAX_DECISIONS: u64 = 64 * 1024 * 1024;

/// A blob read whole within the same deadline as the other reads: the shared helper's default stops at
/// 1 MiB, and the blob of a long decisions log is more.
fn git_blob(dir: &Path, blob: &str) -> Result<Vec<u8>, String> {
    let mut command = Command::new("git");
    pg_worker_core::host_command::isolate_git(&mut command);
    command
        .arg("--no-replace-objects")
        .arg("-C")
        .arg(dir)
        .args(["cat-file", "blob", blob]);
    pg_worker_core::host_command::output_up_to(
        &mut command,
        std::time::Duration::from_secs(60),
        MAX_DECISIONS,
    )
    .map_err(|_| "git cat-file failed or took too long".to_owned())
}

/// The decisions document as committed on `origin/main` of its repository: the branch is fetched into a ref
/// private to this call, resolved to one commit there, and the blob is read from that commit. A failed fetch or
/// read is an error; the local file, a local branch or tag of the same name, or a refresh by another process
/// never stands in for it. A configured symlink is followed like the Keeper does, and the file need not exist
/// in the worktree.
pub(in crate::land) fn read_decisions_origin(path: &Path) -> Result<String, String> {
    let unreadable = || "the decisions document is not readable on origin/main".to_owned();
    let resolved = match path.canonicalize() {
        Ok(real) => real,
        Err(_) => {
            let name = path.file_name().ok_or_else(unreadable)?;
            let parent = path
                .parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or(Path::new("."));
            parent.canonicalize().map_err(|_| unreadable())?.join(name)
        }
    };
    let parent = resolved.parent().ok_or_else(unreadable)?;
    let root = String::from_utf8(git(parent, &["rev-parse", "--show-toplevel"])?)
        .map_err(|_| unreadable())?;
    let root = Path::new(root.trim())
        .canonicalize()
        .map_err(|_| unreadable())?;
    let relative = resolved
        .strip_prefix(&root)
        .map_err(|_| unreadable())?
        .to_owned();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let private = format!("refs/pg-evidence/{}-{nanos}", std::process::id());
    let read = (|| {
        git(
            &root,
            &["fetch", "origin", &format!("+refs/heads/main:{private}")],
        )?;
        let commit = String::from_utf8(git(
            &root,
            &["rev-parse", "--verify", &format!("{private}^{{commit}}")],
        )?)
        .map_err(|_| unreadable())?;
        let blob = format!("{}:{}", commit.trim(), relative.to_string_lossy());
        let size =
            String::from_utf8(git(&root, &["cat-file", "-s", &blob])?).map_err(|_| unreadable())?;
        if size
            .trim()
            .parse::<u64>()
            .map_or(true, |n| n > MAX_DECISIONS)
        {
            return Err("the decisions document is larger than the evidence reads".into());
        }
        String::from_utf8(git_blob(&root, &blob)?).map_err(|_| unreadable())
    })();
    let _ = git(&root, &["update-ref", "-d", &private]);
    read
}
