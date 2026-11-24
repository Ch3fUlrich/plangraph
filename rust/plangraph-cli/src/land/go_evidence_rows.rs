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

/// The shape of a decision id, as a first filter of the rows: `go_verify::verify` judges the id again.
fn decision_id(id: &str) -> bool {
    id.strip_prefix("D-")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

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

/// The approvals and Gate-Edit grants of the decisions log for this repository and pull request.
pub(in crate::land) fn rows(text: &str, repo: &str, pr: u64) -> (Vec<Approval>, Vec<Grant>) {
    let name = repo.rsplit('/').next().unwrap_or(repo);
    let grant = Regex::new(r"^GATE-EDIT\s+(\S+)\s+GO-HEAD\s+([0-9a-fA-F]{40})\s*$").unwrap();
    let (mut approvals, mut grants) = (Vec::new(), Vec::new());
    for line in prose_lines(text) {
        let cells: Vec<&str> = line.trim().split('|').map(str::trim).collect();
        let [_, d_id, project, decision, by, ..] = cells[..] else {
            continue;
        };
        if !decision_id(d_id) || (project != name && project != repo) {
            continue;
        }
        if let Some(c) = grant.captures(strip_prefix(decision)) {
            if by.eq_ignore_ascii_case("operator") {
                grants.push(Grant {
                    d_id: d_id.into(),
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
                    d_id: d_id.into(),
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

/// The decisions document as committed on `origin/main` of its repository: the branch is fetched into
/// `refs/remotes/origin/main`, resolved to one commit, and the blob is read from that commit. A failed fetch or
/// read is an error; the local file, a local branch or tag of the same name, never stands in for it, and the
/// file need not exist in the worktree.
pub(in crate::land) fn read_decisions_origin(path: &Path) -> Result<String, String> {
    let unreadable = || "the decisions document is not readable on origin/main".to_owned();
    let name = path.file_name().ok_or_else(unreadable)?;
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."))
        .canonicalize()
        .map_err(|_| unreadable())?;
    let root = String::from_utf8(git(&parent, &["rev-parse", "--show-toplevel"])?)
        .map_err(|_| unreadable())?;
    let root = Path::new(root.trim())
        .canonicalize()
        .map_err(|_| unreadable())?;
    let relative = parent
        .join(name)
        .strip_prefix(&root)
        .map_err(|_| unreadable())?
        .to_owned();
    git(
        &root,
        &[
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ],
    )?;
    let commit = String::from_utf8(git(
        &root,
        &["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
    )?)
    .map_err(|_| unreadable())?;
    let blob = format!("{}:{}", commit.trim(), relative.to_string_lossy());
    String::from_utf8(git(&root, &["cat-file", "blob", &blob])?).map_err(|_| unreadable())
}
