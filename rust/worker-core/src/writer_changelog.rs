//! Changelog-entry rule for S1b; the adjacent writer_docs_map module owns S1c.
use crate::workspace_resolver::git_command;
use regex::Regex;
use std::{
    collections::{HashMap, HashSet},
    path::Path,
};

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

fn entries(text: &str) -> HashMap<String, usize> {
    let mut found = HashMap::new();
    for raw in lines(text) {
        let line = raw.trim();
        if !line.is_empty() && !line.starts_with('#') {
            *found.entry(line.to_owned()).or_default() += 1;
        }
    }
    found
}

fn entry_added(root: &Path, base: &str, head: &str, path: &str) -> bool {
    let read = |rev| git(root, &["show", &format!("{rev}:{path}")]).unwrap_or_default();
    let before = entries(&read(base));
    entries(&read(head))
        .iter()
        .any(|(line, count)| *count > before.get(line).copied().unwrap_or_default())
}

fn changelog_lines(root: &Path, rev: &str) -> Result<HashMap<String, usize>, String> {
    if git(root, &["ls-tree", "--name-only", rev, "--", "CHANGELOG.md"])?
        .trim()
        .is_empty()
    {
        return Ok(HashMap::new());
    }
    let text = git(root, &["show", &format!("{rev}:CHANGELOG.md")])?;
    let mut found = HashMap::new();
    for line in text.split('\n') {
        *found.entry(line.to_owned()).or_default() += 1;
    }
    Ok(found)
}

fn changelog_edited(root: &Path, sha: &str) -> Result<bool, String> {
    let changed = git(
        root,
        &[
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-m",
            "--no-renames",
            sha,
            "--",
            "CHANGELOG.md",
        ],
    )?;
    if changed.trim().is_empty() {
        return Ok(false);
    }
    let ancestry = git(root, &["rev-list", "--parents", "-n", "1", sha])?;
    let parents: Vec<_> = ancestry.split_whitespace().skip(1).collect();
    if parents.len() < 2 {
        return Ok(true);
    }
    let mut args = vec!["merge-base", "--octopus"];
    args.extend(&parents);
    let base = changelog_lines(root, git(root, &args)?.trim())?;
    let inherited = parents
        .iter()
        .map(|p| changelog_lines(root, p))
        .collect::<Result<Vec<_>, _>>()?;
    let merged = changelog_lines(root, sha)?;
    let keys: HashSet<_> = base
        .keys()
        .chain(merged.keys())
        .chain(inherited.iter().flat_map(|p| p.keys()))
        .collect();
    for key in keys {
        let baseline = base.get(key).copied().unwrap_or_default();
        let counts: Vec<_> = inherited
            .iter()
            .map(|p| p.get(key).copied().unwrap_or_default())
            .collect();
        let added: Vec<_> = counts.iter().map(|n| n.saturating_sub(baseline)).collect();
        // Permit parent deletions from the base and coalesced or combined
        // additions, but reject lost additions and newly invented lines.
        let lower = baseline.min(*counts.iter().min().unwrap()) + added.iter().max().unwrap();
        let upper = baseline.min(*counts.iter().max().unwrap()) + added.iter().sum::<usize>();
        let count = merged.get(key).copied().unwrap_or_default();
        if !(lower..=upper).contains(&count) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn check_release_edits(root: &Path, base: &str, head: &str) -> Result<(), String> {
    let release = Regex::new(r"(?im)^Release:[ \t]*\S.*$").unwrap();
    let commits = git(root, &["rev-list", &format!("{base}..{head}")])?;
    for sha in commits.split_whitespace() {
        if changelog_edited(root, sha)? {
            let trailers = git(
                root,
                &["log", "-1", "--format=%(trailers:only,unfold)", sha],
            )?;
            if !release.is_match(&trailers) {
                return Err(format!(
                    "commit {} edits CHANGELOG.md outside a release; use changelog.d/<ITEM>.md",
                    &sha[..8]
                ));
            }
        }
    }
    Ok(())
}

pub(super) fn check(root: &Path, base: &str, head: &str) -> Result<(), String> {
    let base = git(root, &["merge-base", base, head])?;
    let base = base.trim();
    check_release_edits(root, base, head)?;
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
        }) && entry_added(root, base, head, path)
    });
    if paths.iter().any(|p| !is_doc_path(p)) && !fragment && !(skipped && !uncovered) {
        return Err("non-doc change needs an entry or per-commit skip reason".into());
    }
    Ok(())
}
