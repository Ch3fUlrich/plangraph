//! Repository-local instruction discovery and count-ratcheted lint checks.
use regex::Regex;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs, io,
    path::{Component, Path, PathBuf},
    sync::LazyLock,
};

pub const HISTORY_MARKERS: &[&str] = &[
    "superseded",
    "formerly",
    "was previously",
    "transitional",
    r"until\s+\S+\s+migrates",
];
static HISTORY: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"(?i)\b(?:{})\b", HISTORY_MARKERS.join("|"))).unwrap());
static LINKS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+"[^"]*")?\s*\)|`([^`\n]+)`"#).unwrap()
});

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Finding {
    pub path: String,
    pub line: usize,
    pub check: &'static str,
    pub message: String,
}

fn name(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn finding(path: &Path, line: usize, check: &'static str, message: String) -> Finding {
    Finding {
        path: name(path),
        line,
        check,
        message,
    }
}

fn discover(dir: &Path, files: &mut BTreeSet<PathBuf>) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        if kind.is_dir() && entry.file_name() != ".git" {
            discover(&entry.path(), files)?;
        } else if kind.is_file()
            && matches!(
                entry.file_name().to_str(),
                Some("CLAUDE.md" | "AGENTS.md" | "SKILL.md")
            )
        {
            files.insert(entry.path());
        }
    }
    Ok(())
}

fn local_target(raw: &str, code: bool) -> Option<&str> {
    let target = raw.split(['#', '?']).next()?.trim();
    let target = if code && !target.contains("://") {
        target
            .split_once(':')
            .filter(|(p, _)| p.len() > 1)
            .map_or(target, |(p, _)| p)
    } else {
        target
    };
    if target.is_empty() || target.starts_with(['/', '\\', '~']) || target.contains(':') {
        return None;
    }
    if code
        && (target.chars().any(char::is_whitespace)
            || target.contains(['*', '{', '}', '<', '>', '$', '|'])
            || (!target.contains('/') && Path::new(target).extension().is_none()))
    {
        return None;
    }
    Some(target)
}

fn resolve(root: &Path, source: &Path, target: &str, code: bool) -> Option<PathBuf> {
    let candidates = [source.parent()?.join(target), root.join(target)];
    candidates
        .into_iter()
        .take(if code { 2 } else { 1 })
        .find_map(|p| p.canonicalize().ok().filter(|p| p.starts_with(root)))
}

/// Returns only failures, sorted by repo-relative path, line, check and message.
/// Relative allowlist paths are rooted at `root`; malformed entries are I/O errors.
pub fn lint(root: &Path, allowlist: Option<&Path>) -> io::Result<Vec<Finding>> {
    let root = root.canonicalize()?;
    let mut pending = BTreeSet::new();
    discover(&root, &mut pending)?;
    let mut visited = BTreeSet::new();
    let mut hits = Vec::new();
    while let Some(file) = pending.pop_first() {
        let file = file.canonicalize()?;
        if !visited.insert(file.clone()) {
            continue;
        }
        let text = match fs::read_to_string(&file) {
            Ok(text) => text,
            Err(e) if e.kind() == io::ErrorKind::InvalidData => continue,
            Err(e) => return Err(e),
        };
        let rel = file.strip_prefix(&root).unwrap();
        for (index, line) in text.lines().enumerate() {
            if HISTORY.is_match(line) {
                hits.push(finding(
                    rel,
                    index + 1,
                    "history-note",
                    "history/transitional note".into(),
                ));
            }
            for capture in LINKS.captures_iter(line) {
                let code = capture.get(3).is_some();
                let raw = capture
                    .get(1)
                    .or(capture.get(2))
                    .or(capture.get(3))
                    .unwrap()
                    .as_str();
                let Some(target) = local_target(raw, code) else {
                    continue;
                };
                match resolve(&root, &file, target, code) {
                    Some(path) if path.is_file() => {
                        pending.insert(path);
                    }
                    Some(_) => (),
                    None => hits.push(finding(
                        rel,
                        index + 1,
                        "dead-link",
                        format!("missing local path: {target}"),
                    )),
                }
            }
        }
    }
    hits.sort();
    let Some(allowlist) = allowlist else {
        return Ok(hits);
    };
    let allowlist = root.join(allowlist).canonicalize()?;
    let rel = allowlist
        .strip_prefix(&root)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "allowlist must be under root"))?;
    let mut allowed = BTreeMap::new();
    for (index, line) in fs::read_to_string(&allowlist)?.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let fields: Vec<_> = line.split('|').map(str::trim).collect();
        let invalid = || {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "{}:{}: invalid or duplicate path|check|count",
                    name(rel),
                    index + 1
                ),
            )
        };
        if fields.len() != 3
            || fields[0].is_empty()
            || Path::new(fields[0])
                .components()
                .any(|c| !matches!(c, Component::Normal(_)))
            || !matches!(fields[1], "history-note" | "dead-link")
        {
            return Err(invalid());
        }
        let count = fields[2].parse::<usize>().map_err(|_| invalid())?;
        if allowed
            .insert(
                (fields[0].to_owned(), fields[1].to_owned()),
                (count, index + 1),
            )
            .is_some()
        {
            return Err(invalid());
        }
    }
    let mut actual = BTreeMap::new();
    hits.retain(|hit| {
        let key = (hit.path.clone(), hit.check.to_owned());
        let count = actual.entry(key.clone()).or_insert(0);
        *count += 1;
        *count > allowed.get(&key).map_or(0, |(budget, _)| *budget)
    });
    for (key, (budget, line)) in allowed {
        let count = actual.get(&key).copied().unwrap_or(0);
        if budget > count {
            hits.push(finding(
                rel,
                line,
                "stale-allowlist",
                format!("{}|{}: allowed {budget}, actual {count}", key.0, key.1),
            ));
        }
    }
    hits.sort();
    Ok(hits)
}
