//! Rule source: tools/check_file_size.py (measure, count_lines, read_allow,
//! parse_baseline, limit_violations, ratchet_violations). Only run-changed tracked
//! files are measured here; CI intentionally still measures the entire checkout.
use crate::workspace_resolver::git_command;
use std::{collections::BTreeMap, path::Path};

fn git(root: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let out = git_command(root)
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "git {} failed: {}",
            args[0],
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(out.stdout)
}

// Python str.splitlines includes these separators, and treats CRLF as one.
fn lines(text: &str) -> Vec<&str> {
    let mut result = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if matches!(
            c,
            '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}'
                ..='\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
        ) {
            result.push(&text[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && chars.peek().is_some_and(|(_, c)| *c == '\n') {
                start = chars.next().unwrap().0 + 1;
            }
        }
    }
    if start < text.len() {
        result.push(&text[start..]);
    }
    result
}

fn config(root: &Path, name: &str) -> Result<String, String> {
    let path = root.join(name);
    if !path.is_file() {
        return Ok(String::new());
    }
    std::fs::read(path)
        .map(|b| String::from_utf8_lossy(&b).into_owned())
        .map_err(|e| format!("{name}: {e}"))
}

fn baseline(text: &str) -> Result<BTreeMap<String, i64>, String> {
    let mut counts = BTreeMap::new();
    for line in lines(text).into_iter().map(str::trim) {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let fields: Vec<_> = line.split_whitespace().collect();
        if fields.len() != 2 {
            return Err(".filesize-baseline: want path count".into());
        }
        let count = fields[1]
            .parse()
            .map_err(|_| ".filesize-baseline: invalid count")?;
        if counts.insert(fields[0].into(), count).is_some() {
            return Err(format!(".filesize-baseline: duplicate {}", fields[0]));
        }
    }
    Ok(counts)
}

pub(super) fn check(root: &Path, base: &str, head: &str) -> Result<(), String> {
    if base.is_empty() {
        return Err("no base".into());
    }
    // The recorded run base maps to Python's FILESIZE_REQUIRE_BASE, not its
    // optional local origin/main -> HEAD fallback. Resolve both range endpoints.
    let resolved = git(
        root,
        &["rev-parse", "--verify", &format!("{base}^{{commit}}")],
    )?;
    let resolved_head = git(
        root,
        &["rev-parse", "--verify", &format!("{head}^{{commit}}")],
    )?;
    if resolved == resolved_head {
        return Ok(());
    }
    let allow = config(root, ".filesize-allow")?;
    let mut allowed = std::collections::BTreeSet::new();
    for line in lines(&allow).into_iter().map(str::trim) {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut fields = line.splitn(2, char::is_whitespace);
        let path = fields.next().unwrap();
        if fields.next().is_none_or(|s| s.trim().is_empty()) {
            return Err(format!(".filesize-allow: {path} needs a reason"));
        }
        if !allowed.insert(path) {
            return Err(format!(".filesize-allow: duplicate {path}"));
        }
    }
    let counts = baseline(&config(root, ".filesize-baseline")?)?;
    match git(root, &["show", &format!("{base}:.filesize-baseline")]) {
        Ok(bytes) => {
            let previous = baseline(&String::from_utf8_lossy(&bytes))?;
            for (path, count) in &counts {
                if previous.get(path).is_none_or(|old| count > old) {
                    return Err(format!(".filesize-baseline: {path} added or raised"));
                }
            }
        }
        Err(error) => {
            // A missing path is optional only when no current counts need a
            // ratchet. Object corruption and other git failures must still hold.
            let entry = git(root, &["ls-tree", base, "--", ".filesize-baseline"])?;
            if !entry.is_empty() {
                return Err(error);
            }
            if !counts.is_empty() {
                return Err(".filesize-baseline: no baseline at base".into());
            }
        }
    }
    let changed = git(
        root,
        &[
            "diff",
            "--name-only",
            "--diff-filter=d",
            "-z",
            base,
            head,
            "--",
        ],
    )?;
    let changed: std::collections::BTreeSet<_> = changed.split(|b| *b == 0).collect();
    // Never use quoted ls-files output: non-ASCII names must name real files.
    let tracked = git(root, &["ls-tree", "-r", "--name-only", "-z", head])?;
    for raw in tracked.split(|b| *b == 0).filter(|p| !p.is_empty()) {
        if !changed.contains(raw) {
            continue;
        }
        let path = String::from_utf8_lossy(raw);
        // Python decodes git output and content with errors="replace". Invalid
        // names therefore refer to the replacement-character path, not raw bytes.
        let full = root.join(path.as_ref());
        if !full.is_file() {
            return Err(format!("{path}: missing changed path at head"));
        }
        if git(root, &["rev-parse", "HEAD"])? != git(root, &["rev-parse", head])? {
            return Err(format!("{path}: checkout is not at head"));
        }
        let name = path.rsplit('/').next().unwrap();
        if allowed.contains(path.as_ref())
            || path
                .split('/')
                .any(|p| matches!(p, "vendor" | "third_party" | "node_modules"))
            || [".lock", ".json", ".svg"].iter().any(|s| name.ends_with(s))
            || name.contains(".min.")
        {
            continue;
        }
        let bytes = std::fs::read(full).map_err(|e| format!("{path}: {e}"))?;
        if bytes.iter().take(8192).any(|b| *b == 0) {
            continue;
        }
        let count = lines(&String::from_utf8_lossy(&bytes)).len();
        let cap = 800.max(*counts.get(path.as_ref()).unwrap_or(&0));
        if count as i64 > cap {
            return Err(format!("{path} {count} lines > {cap}"));
        }
    }
    Ok(())
}
