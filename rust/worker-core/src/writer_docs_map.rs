//! S1c docs-map gate: committed head policy over the run-base merge-base range.
use crate::workspace_resolver::git_command;
use chrono::{Datelike, Local, NaiveDate, Weekday};
use regex::Regex;
use std::path::Path;

type Rule = (usize, String, Vec<String>);

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

fn iso_date(text: &str) -> Option<NaiveDate> {
    // Python date.fromisoformat also accepts compact dates and ISO week dates.
    let syntax = Regex::new(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8}|[0-9]{4}-W[0-9]{2}(?:-[1-7])?|[0-9]{4}W[0-9]{2}[1-7]?)$").unwrap();
    if !syntax.is_match(text) {
        return None;
    }
    let compact = text.replace('-', "");
    let year = compact[..4].parse().ok()?;
    if year == 0 {
        return None;
    }
    if compact.contains('W') {
        let day = compact.get(7..).filter(|d| !d.is_empty()).unwrap_or("1");
        let weekday = [
            Weekday::Mon,
            Weekday::Tue,
            Weekday::Wed,
            Weekday::Thu,
            Weekday::Fri,
            Weekday::Sat,
            Weekday::Sun,
        ][day.parse::<usize>().ok()? - 1];
        NaiveDate::from_isoywd_opt(year, compact[5..7].parse().ok()?, weekday)
            .filter(|d| (1..=9999).contains(&d.year()))
    } else {
        NaiveDate::from_ymd_opt(
            year,
            compact[4..6].parse().ok()?,
            compact[6..].parse().ok()?,
        )
    }
}

pub(super) fn parse_docs_map(text: &str) -> (Option<NaiveDate>, Vec<Rule>, Vec<String>) {
    let mut date = None;
    let mut rules = Vec::new();
    let mut errors = Vec::new();
    let directive = Regex::new(r"^#\s*enforce-after:\s*(\S+)").unwrap();
    // Match Python splitlines, with CRLF treated as a single boundary.
    let boundaries = Regex::new(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\x{85}\x{2028}\x{2029}]").unwrap();
    for (i, raw) in boundaries.split(text).enumerate() {
        let line = raw.trim();
        let prefix = format!(".docs-map:{}", i + 1);
        if line.is_empty() {
            continue;
        }
        if line.starts_with('#') {
            if let Some(c) = directive.captures(line) {
                match iso_date(&c[1]) {
                    Some(d) => date = Some(d),
                    None => errors.push(format!(
                        "{prefix}: malformed enforce-after date: '{}'",
                        &c[1]
                    )),
                }
            }
            continue;
        }
        if let Some((pattern, docs)) = line.split_once(" -> ") {
            let docs: Vec<_> = docs
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect();
            if !pattern.trim().is_empty() && !docs.is_empty() {
                rules.push((i + 1, pattern.trim().to_owned(), docs));
                continue;
            }
        }
        errors.push(format!(
            "{prefix}: malformed rule: want '<glob> -> <doc>', got: '{line}'"
        ));
    }
    (date, rules, errors)
}

pub(super) fn glob_to_regex(mut pattern: &str) -> Result<Regex, String> {
    let mut out = String::from(r"\A");
    while !pattern.is_empty() {
        if let Some(rest) = pattern.strip_prefix("**/") {
            out.push_str("(?:.*/)?");
            pattern = rest;
        } else if let Some(rest) = pattern.strip_prefix("**") {
            out.push_str(".*");
            pattern = rest;
        } else {
            let c = pattern.chars().next().unwrap();
            out.push_str(&match c {
                '*' => "[^/]*".into(),
                '?' => "[^/]".into(),
                _ => regex::escape(&c.to_string()),
            });
            pattern = &pattern[c.len_utf8()..];
        }
    }
    Regex::new(&(out + r"\z")).map_err(|_| "docs-map glob compilation failed".into())
}

pub(super) fn check(root: &Path, base: &str, head: &str) -> Result<(), String> {
    check_on(root, base, head, Local::now().date_naive())
}

pub(super) fn check_on(
    root: &Path,
    base: &str,
    head: &str,
    today: NaiveDate,
) -> Result<(), String> {
    let base = git(root, &["merge-base", base, head])?;
    let listed = git(root, &["ls-tree", "--name-only", head, "--", ".docs-map"])?;
    if listed.trim().is_empty() {
        return Ok(());
    }
    let text = git(root, &["show", &format!("{head}:.docs-map")])?;
    let (date, rules, mut errors) = parse_docs_map(&text);
    let changed = git(
        root,
        &[
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            base.trim(),
            head,
        ],
    )?;
    let paths: Vec<_> = changed
        .split('\0')
        .filter(|p| !p.is_empty())
        .map(|p| p.trim().replace('\\', "/"))
        .collect();
    if date.is_none_or(|d| today >= d) {
        for (_, pattern, docs) in rules {
            let rx = glob_to_regex(&pattern)?;
            for path in &paths {
                if rx.is_match(path) && !docs.iter().any(|d| paths.contains(d)) {
                    errors.push(format!(
                        "{path} -> {} (matches '{pattern}')",
                        docs.join(", ")
                    ));
                }
            }
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}
