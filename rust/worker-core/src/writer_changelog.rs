//! Changelog-entry rule for S1b; docs-map enforcement moves into Rust in S1c.
use crate::workspace_resolver::git_command;
use regex::Regex;
use std::{collections::HashMap, path::Path};

fn git(root: &Path, args: &[&str]) -> Result<String, String> {
    let out = git_command(root)
        .args(args)
        .output()
        .map_err(|_| "git unavailable".to_string())?;
    if !out.status.success() {
        return Err(format!("git {} failed", args[0]));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

fn is_doc_path(path: &str) -> bool {
    let path = path.replace('\\', "/");
    path.ends_with(".md")
        || path == "docs"
        || path.starts_with("docs/")
        || path == "LICENSE"
        || path == ".docs-map"
}

fn lines(text: &str) -> impl Iterator<Item = &str> {
    // Python str.splitlines(), including Unicode line boundaries.
    text.split([
        '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
        '\u{2029}',
    ])
}

fn entries(text: &str, unreleased_only: bool) -> HashMap<String, usize> {
    let heading = Regex::new(r"(?i)^##\s+\[?unreleased\]?\s*$").unwrap();
    let section = Regex::new(r"^##\s+").unwrap();
    let mut inside = !unreleased_only;
    let mut found = HashMap::new();
    for raw in lines(text) {
        let line = raw.trim();
        if unreleased_only && heading.is_match(line) {
            inside = true;
        } else if unreleased_only && section.is_match(line) {
            inside = false;
        } else if inside && !line.is_empty() && !line.starts_with('#') {
            *found.entry(line.to_owned()).or_default() += 1;
        }
    }
    found
}

fn entry_added(root: &Path, base: &str, head: &str, path: &str, unreleased: bool) -> bool {
    let read = |rev| git(root, &["show", &format!("{rev}:{path}")]).unwrap_or_default();
    let before = entries(&read(base), unreleased);
    entries(&read(head), unreleased)
        .iter()
        .any(|(line, count)| *count > before.get(line).copied().unwrap_or_default())
}

pub(super) fn check(root: &Path, base: &str, head: &str) -> Result<(), String> {
    let base = git(root, &["merge-base", base, head])?;
    let base = base.trim();
    let paths = git(
        root,
        &["diff", "--name-only", "--no-renames", "-z", base, head],
    )?;
    let paths: Vec<_> = paths.split('\0').filter(|p| !p.is_empty()).collect();
    let commits = git(
        root,
        &[
            "rev-list",
            "--no-merges",
            "--reverse",
            &format!("{base}..{head}"),
        ],
    )?;
    let skip = Regex::new(r"(?i)^\s*changelog\s*:\s*skip(?:\s*\((.*?)\))?\s*$").unwrap();
    let mut skipped = false;
    let mut uncovered = false;
    for sha in commits.split_whitespace() {
        let trailers = git(
            root,
            &["log", "-1", "--format=%(trailers:only,unfold)", sha],
        )?;
        let mut reason = false;
        let mut empty_reason = false;
        for line in lines(&trailers) {
            if let Some(capture) = skip.captures(line) {
                if capture
                    .get(1)
                    .is_some_and(|m| !m.as_str().trim().is_empty())
                {
                    reason = true;
                } else {
                    empty_reason = true;
                }
            }
        }
        let changed = git(
            root,
            &[
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "--no-renames",
                "-z",
                sha,
            ],
        )?;
        if changed
            .split('\0')
            .filter(|p| !p.is_empty())
            .any(|p| !is_doc_path(p))
        {
            if empty_reason {
                return Err(format!("commit {} has skip with empty reason", &sha[..8]));
            }
            skipped |= reason;
            uncovered |= !reason;
        }
    }
    let fragment = paths.iter().any(|path| {
        path.strip_prefix("changelog.d/").is_some_and(|name| {
            !name.contains('/') && name.ends_with(".md") && !name.eq_ignore_ascii_case("readme.md")
        }) && entry_added(root, base, head, path, false)
    });
    if paths.iter().any(|p| !is_doc_path(p))
        && !entry_added(root, base, head, "CHANGELOG.md", true)
        && !fragment
        && !(skipped && !uncovered)
    {
        return Err("non-doc change needs an entry or per-commit skip reason".into());
    }
    Ok(())
}
