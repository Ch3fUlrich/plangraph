//! Fold the per-item documentation fragments (`docs/<area>/<ITEM>.md`) into their shared reference once
//! per train, on main. Each fragment becomes a marked block at the end of its reference; the block is
//! the folding's idempotency record, so a crash between the write and the removal repeats safely.
//! Order is by area, then fragment name. Existing reference content is never rewritten, and only a
//! fragment whose block is in the reference is removed. A fragment that would push its reference past
//! the line cap stays where it is.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

/// A fragment folder and the reference it folds into. The docs-map checks accept the same areas.
pub const AREAS: [(&str, &str); 3] = [
    ("contributing", "CONTRIBUTING.md"),
    ("keeper", "docs/KEEPER.md"),
    ("spawner", "docs/SPAWNER.md"),
];

/// A folded paragraph line longer than this is split at sentence ends.
pub const MAX_LINE: usize = 600;

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Report {
    pub folded: Vec<String>,
    /// Fragment path and why it stayed.
    pub kept: Vec<(String, String)>,
}

fn valid_name(name: &str) -> Option<&str> {
    let stem = name.strip_suffix(".md")?;
    let mut chars = stem.chars();
    let first = chars.next()?;
    (stem != "README"
        && first.is_ascii_uppercase()
        && chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_' || c == '-'))
    .then_some(stem)
}

/// Split every line over `MAX_LINE` characters after a sentence end (`. `), keeping the words.
fn split_paragraphs(text: &str) -> String {
    let mut out = Vec::new();
    for line in text.lines() {
        let mut rest = line.trim_end();
        while rest.chars().count() > MAX_LINE {
            let limit = rest
                .char_indices()
                .nth(MAX_LINE)
                .map_or(rest.len(), |(i, _)| i);
            let cut = rest[..limit]
                .rmatch_indices(". ")
                .next()
                .map(|(i, _)| i + 1)
                .or_else(|| rest[..limit].rfind(' '));
            match cut {
                Some(i) if i > 0 => {
                    out.push(rest[..i].trim_end().to_owned());
                    rest = rest[i..].trim_start();
                }
                _ => break,
            }
        }
        out.push(rest.to_owned());
    }
    out.join("\n")
}

fn block(item: &str, body: &str) -> String {
    format!(
        "<!-- fragment:{item} -->\n{}\n<!-- /fragment:{item} -->\n",
        split_paragraphs(body.trim())
    )
}

fn lines(text: &str) -> usize {
    text.lines().count()
}

/// `DOC_CAPS` minus `DOC_HEADROOM` from the repository's size checker; empty when it has neither.
pub fn caps(root: &Path) -> BTreeMap<String, usize> {
    let Ok(text) = std::fs::read_to_string(root.join("tools/check_file_size.py")) else {
        return BTreeMap::new();
    };
    let headroom = regex::Regex::new(r"(?m)^DOC_HEADROOM\s*=\s*(\d+)")
        .unwrap()
        .captures(&text)
        .and_then(|c| c[1].parse::<usize>().ok())
        .unwrap_or(0);
    let Some(map) = regex::Regex::new(r"(?m)^DOC_CAPS\s*=\s*\{([^}]*)\}")
        .unwrap()
        .captures(&text)
    else {
        return BTreeMap::new();
    };
    regex::Regex::new(r#""([^"]+)"\s*:\s*(\d+)"#)
        .unwrap()
        .captures_iter(&map[1])
        .filter_map(|c| {
            Some((
                c[1].to_owned(),
                c[2].parse::<usize>().ok()?.checked_sub(headroom)?,
            ))
        })
        .collect()
}

/// Fold every fragment under `root/docs/<area>/`. With `apply` false nothing is written.
pub fn fold(root: &Path, caps: &BTreeMap<String, usize>, apply: bool) -> Result<Report, String> {
    let mut report = Report::default();
    for (area, reference) in AREAS {
        let dir = root.join("docs").join(area);
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        let mut names: Vec<String> = entries
            .filter_map(Result::ok)
            .filter(|e| e.path().is_file())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        let ref_path = root.join(reference);
        let mut text =
            std::fs::read_to_string(&ref_path).map_err(|e| format!("{reference}: {e}"))?;
        let mut folded_here: Vec<(PathBuf, String)> = Vec::new();
        for name in names {
            let rel = format!("docs/{area}/{name}");
            let Some(item) = valid_name(&name) else {
                report.kept.push((rel, "not an item fragment name".into()));
                continue;
            };
            let body =
                std::fs::read_to_string(dir.join(&name)).map_err(|e| format!("{rel}: {e}"))?;
            if body.trim().is_empty() {
                report.kept.push((rel, "empty".into()));
                continue;
            }
            let wanted = block(item, &body);
            let marker = format!("<!-- fragment:{item} -->\n");
            if let Some(at) = text.find(&marker) {
                let end_marker = format!("<!-- /fragment:{item} -->\n");
                let end = text[at..]
                    .find(&end_marker)
                    .map(|e| at + e + end_marker.len())
                    .ok_or_else(|| format!("{reference}: unterminated block for {item}"))?;
                if text[at..end] != wanted {
                    report
                        .kept
                        .push((rel, format!("{reference} already holds a different {item}")));
                    continue;
                }
            } else {
                let mut next = text.clone();
                if !next.ends_with("\n\n") {
                    next.push_str(if next.ends_with('\n') { "\n" } else { "\n\n" });
                }
                next.push_str(&wanted);
                if let Some(cap) = caps.get(reference) {
                    if lines(&next) > *cap {
                        report
                            .kept
                            .push((rel, format!("{reference} would pass its {cap}-line cap")));
                        continue;
                    }
                }
                text = next;
            }
            folded_here.push((dir.join(&name), rel));
        }
        if apply && !folded_here.is_empty() {
            write_atomic(&ref_path, &text)?;
        }
        for (path, rel) in folded_here {
            if apply {
                std::fs::remove_file(&path).map_err(|e| format!("{rel}: {e}"))?;
            }
            report.folded.push(rel);
        }
    }
    Ok(report)
}

fn write_atomic(path: &Path, text: &str) -> Result<(), String> {
    use std::io::Write;
    let dir = path.parent().unwrap_or(Path::new("."));
    let mut file = tempfile::NamedTempFile::new_in(dir).map_err(|e| e.to_string())?;
    file.write_all(text.as_bytes()).map_err(|e| e.to_string())?;
    file.persist(path).map_err(|e| e.error.to_string())?;
    Ok(())
}

/// Commits in `base..head` that edit a shared reference directly, per reference: the number the
/// fragments are meant to bring to zero.
pub fn direct_edits(root: &Path, range: &str) -> Result<Vec<(String, usize)>, String> {
    AREAS
        .iter()
        .map(|(_, reference)| {
            let out = Command::new("git")
                .arg("-C")
                .arg(root)
                .args(["log", "--format=%H", range, "--", reference])
                .output()
                .map_err(|e| format!("git: {e}"))?;
            if !out.status.success() {
                return Err(format!(
                    "git log {range}: {}",
                    String::from_utf8_lossy(&out.stderr).trim()
                ));
            }
            Ok((
                (*reference).to_owned(),
                String::from_utf8_lossy(&out.stdout).lines().count(),
            ))
        })
        .collect()
}

pub(super) fn run(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let root = PathBuf::from(flags.get("repo-dir").ok_or(
        "usage: plangraph land-train fold-docs --repo-dir DIR [--check 1 | --measure BASE..HEAD]",
    )?);
    if let Some(range) = flags.get("measure") {
        return Ok(direct_edits(&root, range)?
            .into_iter()
            .map(|(reference, n)| format!("{reference}: {n} direct edit(s) in {range}"))
            .collect());
    }
    let apply = !flags.contains_key("check");
    let report = fold(&root, &caps(&root), apply)?;
    let mut out: Vec<String> = report
        .folded
        .iter()
        .map(|f| format!("{} {f}", if apply { "folded" } else { "would fold" }))
        .collect();
    out.extend(
        report
            .kept
            .iter()
            .map(|(f, why)| format!("kept {f}: {why}")),
    );
    Ok(out)
}
