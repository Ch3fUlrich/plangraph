//! New-code language policy. All authority is read from committed revisions.
mod part_entry;
mod plan_items;
mod test_sources;

use regex::Regex;
use std::{collections::HashMap, env, path::Path, process::Command};

const ALLOWLIST: &str = "tools/new_code_allowlist.txt";
const INDEX: &str = "tools/new_code_decisions_index.txt";
const BLOCKED: &[&str] = &[
    ".bash", ".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".py", ".sh", ".ts", ".tsx",
];

fn git(args: &[&str]) -> Result<String, String> {
    let out = Command::new("git")
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_owned());
    }
    String::from_utf8(out.stdout).map_err(|e| e.to_string())
}

fn blob(revision: &str, path: &str) -> Result<String, String> {
    git(&["show", &format!("{revision}:{path}")])
}

fn matches(path: &str, pattern: &str) -> bool {
    fn segment(text: &str, glob: &str) -> bool {
        let mut expression = String::from("^");
        let mut chars = glob.chars().peekable();
        while let Some(c) = chars.next() {
            match c {
                '*' => expression.push_str(".*"),
                '?' => expression.push('.'),
                '[' => {
                    let mut class = String::new();
                    if chars.peek() == Some(&'!') {
                        chars.next();
                        class.push('^');
                    }
                    let mut closed = false;
                    for next in chars.by_ref() {
                        if next == ']' {
                            closed = true;
                            break;
                        }
                        class.push(next);
                    }
                    if !closed {
                        return false;
                    }
                    expression.push('[');
                    expression.push_str(&class);
                    expression.push(']');
                }
                _ => expression.push_str(&regex::escape(&c.to_string())),
            }
        }
        expression.push('$');
        Regex::new(&expression).is_ok_and(|r| r.is_match(text))
    }
    fn walk(parts: &[&str], globs: &[&str]) -> bool {
        match globs.split_first() {
            None => parts.is_empty(),
            Some((&"**", rest)) => {
                walk(parts, rest) || (!parts.is_empty() && walk(&parts[1..], globs))
            }
            Some((glob, rest)) => {
                !parts.is_empty() && segment(parts[0], glob) && walk(&parts[1..], rest)
            }
        }
    }
    walk(
        &path.split('/').collect::<Vec<_>>(),
        &pattern.split('/').collect::<Vec<_>>(),
    )
}

fn blocked(path: &str, revision: &str) -> bool {
    let suffix = Path::new(path)
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()));
    if suffix
        .as_ref()
        .is_some_and(|s| BLOCKED.contains(&s.as_str()))
    {
        return true;
    }
    // Bound the read and terminate git rather than materializing large blobs.
    use std::io::Read;
    use std::process::Stdio;
    let Ok(mut child) = Command::new("git")
        .args(["show", &format!("{revision}:{path}")])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return false;
    };
    let mut prefix = Vec::new();
    let read = child
        .stdout
        .take()
        .unwrap()
        .take(256)
        .read_to_end(&mut prefix);
    let _ = child.kill();
    let _ = child.wait();
    if read.is_err() || prefix.contains(&0) {
        return false;
    }
    let Ok(text) = std::str::from_utf8(&prefix) else {
        return false;
    };
    let line = text.lines().next().unwrap_or("");
    line.strip_prefix("#!").is_some_and(|s| {
        Regex::new(r"(?:^|[/\s])(?:python(?:\d+(?:\.\d+)*)?|sh|bash|node)(?:\s|$)")
            .unwrap()
            .is_match(s)
    })
}

fn entries(text: &str) -> impl Iterator<Item = (usize, &str)> {
    text.lines()
        .enumerate()
        .map(|(n, s)| (n + 1, s.trim()))
        .filter(|(_, s)| !s.is_empty() && !s.starts_with('#'))
}

fn run() -> Result<Vec<String>, String> {
    if env::args().nth(1).as_deref() == Some("--test-sources") {
        let root = env::args().nth(2).unwrap_or_else(|| ".".into());
        return test_sources::check(Path::new(&root));
    }
    let mut base = env::var("NEWCODE_BASE").unwrap_or_else(|_| "origin/main".into());
    let mut head = String::from("HEAD");
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--base" => base = args.next().ok_or("--base needs a revision")?,
            "--head" => head = args.next().ok_or("--head needs a revision")?,
            "-h" | "--help" => {
                println!("check-new-code-lang [--base REV] [--head REV]");
                return Ok(vec![]);
            }
            _ => return Err(format!("unknown argument {arg}")),
        }
    }
    let root = git(&["rev-parse", "--show-toplevel"])?;
    env::set_current_dir(root.trim()).map_err(|e| e.to_string())?;
    let base = git(&["rev-parse", "--verify", &format!("{base}^{{commit}}")])?;
    let head = git(&["rev-parse", "--verify", &format!("{head}^{{commit}}")])?;
    let (base, head) = (base.trim(), head.trim());
    if base == head {
        return Err("base is HEAD itself".into());
    }
    let old = git(&["merge-base", base, head])?;
    let old = old.trim();
    let text = blob(head, ALLOWLIST)?;
    let prior = blob(old, ALLOWLIST).unwrap_or_default();
    let mut counts = HashMap::<&str, usize>::new();
    for (_, entry) in entries(&prior) {
        *counts.entry(entry).or_default() += 1;
    }
    // A PR cannot invent its own operator decision: the index comes from base.
    let index = blob(old, INDEX).unwrap_or_default();
    let operators: Vec<_> = entries(&index)
        .filter_map(|(_, line)| {
            let (id, authority) = line.split_once('|')?;
            (authority.trim() == "OPERATOR" && !["D-5840", "D-5830"].contains(&id.trim()))
                .then_some(id.trim())
        })
        .collect();
    let did = Regex::new(r"\bD-[0-9]+\b").unwrap();
    let mut allowed = Vec::new();
    let mut errors = Vec::new();
    for (number, entry) in entries(&text) {
        let Some((pattern, reason)) = entry
            .split_once('|')
            .filter(|(p, r)| !p.trim().is_empty() && !r.trim().is_empty())
        else {
            errors.push(format!("{ALLOWLIST}:{number}: entry needs a glob | reason"));
            continue;
        };
        let count = counts.entry(entry).or_default();
        if *count > 0 {
            *count -= 1;
        } else if !did
            .find_iter(reason)
            .any(|m| operators.contains(&m.as_str()))
        {
            errors.push(format!("{ALLOWLIST}:{number}: added entry needs an OPERATOR D-id from base {INDEX}; D-5840 and D-5830 do not count"));
        }
        allowed.push(pattern.trim());
    }
    let changes = git(&[
        "diff",
        "--find-renames",
        "--find-copies",
        "--find-copies-harder",
        "--diff-filter=ARC",
        "--name-status",
        "-z",
        &format!("{old}..{head}"),
        "--",
    ])?;
    let mut fields = changes.trim_end_matches('\0').split('\0');
    while let Some(status) = fields.next().filter(|s| !s.is_empty()) {
        let original = fields.next().ok_or("malformed diff")?;
        let path = if status.starts_with(['R', 'C']) {
            fields.next().ok_or("malformed rename")?
        } else {
            original
        };
        if status.starts_with('R') && blocked(original, old) {
            continue;
        }
        if blocked(path, head) && !allowed.iter().any(|g| matches(path, g)) {
            errors.push(format!("FAIL {path}: new code must be Rust (D-4691); blocked suffixes: {}; Python/shell/Node shebangs are also blocked", BLOCKED.join(", ")));
        }
    }
    Ok(errors)
}

fn class_search(args: &[String]) -> Result<Vec<String>, String> {
    let train_delta = args.first().is_some_and(|arg| arg == "--train-delta");
    let validate_range = args.first().is_some_and(|arg| arg == "--range");
    let range_commits;
    let revisions = if validate_range {
        let [_, base] = args else {
            return Err("--class-search --range needs a base revision".into());
        };
        range_commits = git(&[
            "log",
            "--no-notes",
            "--format=%H",
            "--grep=^Class-Search:",
            "--end-of-options",
            &format!("{base}..HEAD"),
            "--",
        ])?;
        range_commits.lines().collect::<Vec<_>>()
    } else {
        let revisions = if train_delta { &args[1..] } else { args };
        if revisions.is_empty() {
            return Err("--class-search needs at least one fix revision".into());
        }
        revisions.iter().map(String::as_str).collect()
    };
    let format =
        Regex::new(r"^Class-Search: (?:([0-9a-f]{8}) )?(\S.*) -> ([0-9]+) hits, ([0-9]+) changed$")
            .unwrap();
    let resolution = Regex::new(r"^Train [0-9]+: resolve(?:\s|$)").unwrap();
    let valid_counts = |fields: &regex::Captures<'_>| {
        let hits = fields[3].parse::<usize>();
        let changed = fields[4].parse::<usize>();
        matches!((hits, changed), (Ok(hits), Ok(changed)) if changed <= hits)
    };

    let mut findings = Vec::new();
    for revision in revisions {
        let message = git(&[
            "show",
            "--no-patch",
            "--no-notes",
            "--format=%H%x00%P%x00%s%x00%b",
            "--end-of-options",
            &format!("{revision}^{{commit}}"),
            "--",
        ])?;
        let mut fields = message.splitn(4, '\0');
        let [Some(sha), Some(parents), Some(subject), Some(body)] =
            std::array::from_fn(|_| fields.next())
        else {
            return Err("malformed commit message".into());
        };
        if train_delta && parents.split_whitespace().count() == 1 && resolution.is_match(subject) {
            continue;
        }
        let mut inline = false;
        let mut malformed = Vec::new();
        for line in body
            .lines()
            .filter(|line| line.starts_with("Class-Search:"))
        {
            match format.captures(line) {
                Some(record) if valid_counts(&record) => inline |= record.get(1).is_none(),
                _ => malformed.push(line),
            }
        }
        if (malformed.is_empty() && (inline || validate_range))
            || later_class_search(&format, sha, valid_counts)?
        {
            continue;
        }
        let detail = if malformed.is_empty() {
            String::new()
        } else {
            format!(": {}", malformed.join("; "))
        };
        findings.push(format!(
            "{sha}: missing or invalid Class-Search in fix commit body or a later commit on HEAD{detail}"
        ));
    }
    Ok(findings)
}

fn later_class_search(
    format: &Regex,
    sha: &str,
    valid_counts: impl Fn(&regex::Captures<'_>) -> bool,
) -> Result<bool, String> {
    let prefix = &sha[..8];
    let later = git(&[
        "log",
        "--no-notes",
        "--ancestry-path",
        "--format=%b",
        "--fixed-strings",
        &format!("--grep=Class-Search: {prefix} "),
        &format!("{sha}..HEAD"),
        "--",
    ])?;
    if !later.lines().any(|line| {
        format.captures(line).is_some_and(|record| {
            record.get(1).is_some_and(|short| short.as_str() == prefix) && valid_counts(&record)
        })
    }) {
        return Ok(false);
    }
    let matches = git(&["rev-parse", &format!("--disambiguate={prefix}")])?;
    Ok(matches.trim() == sha)
}

fn main() {
    let args: Vec<_> = env::args().skip(1).collect();
    let result = if args
        .first()
        .is_some_and(|arg| arg == "--plan-item-trailers")
    {
        plan_items::run(&args[1..])
    } else if args.first().is_some_and(|arg| arg == "--part-entry") {
        part_entry::run(&args[1..]).map_err(|e| format!("part-entry: {e}"))
    } else if args.first().is_some_and(|arg| arg == "--class-search") {
        class_search(&args[1..])
    } else {
        run()
    };
    let advisory = args
        .first()
        .is_some_and(|arg| arg == "--plan-item-trailers");
    match result {
        Ok(errors) => {
            for error in &errors {
                println!("{error}");
            }
            std::process::exit(i32::from(!advisory && !errors.is_empty()));
        }
        Err(error) => {
            println!("check-new-code-lang: {error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn glob_segments_and_recursive_patterns() {
        assert!(matches("tests/a/test_x.py", "tests/**/test_*.py"));
        assert!(matches("tests/test_x.py", "tests/**/test_*.py"));
        assert!(!matches("tests/a/test_x.py", "tests/test_*.py"));
        assert!(matches("a1.py", "a[0-9].py"));
    }
}
