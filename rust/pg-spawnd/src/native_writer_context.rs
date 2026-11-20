//! Evidence for the item-context block of the native review brief, read from the writer
//! brief (feeder or batch layout) and the exact diff.
use std::collections::BTreeSet;

const MAX_FIELD: usize = 6000;
const MAX_CALLERS_PER_SYMBOL: usize = 3;

fn heading(line: &str) -> bool {
    let line = line.trim();
    line.starts_with("###")
        || line.starts_with("Plan item:")
        || [
            "Files:",
            "Notes:",
            "Task metadata:",
            "Title:",
            "Why:",
            "How:",
            "Priority:",
            "Done criteria",
            "Done when",
            "Invariant",
            "Plan-Item:",
            "Writer:",
            "Changelog:",
        ]
        .iter()
        .any(|h| line.starts_with(h))
}

/// Every section opened by a line starting with one of `labels` (case-insensitive): the
/// text after the label on that line, then each following line up to the next heading.
/// Multi-line criteria of the feeder layout (`Done criteria:` alone on its line) and the
/// one-line batch layout both come out whole, one entry per item.
fn sections(brief: &str, labels: &[&str]) -> Vec<String> {
    let mut found = Vec::new();
    let mut open: Option<String> = None;
    for line in brief.lines() {
        let lower = line.trim_start().to_ascii_lowercase();
        if let Some(label) = labels.iter().find(|l| lower.starts_with(*l)) {
            found.extend(open.take());
            let rest = line.trim_start()[label.len()..].trim_start_matches([':', ' ']);
            open = Some(rest.to_owned());
        } else if heading(line) {
            found.extend(open.take());
        } else if let Some(text) = open.as_mut() {
            text.push('\n');
            text.push_str(line.trim_end());
        }
    }
    found.extend(open);
    found
        .into_iter()
        .map(|text| text.trim().to_owned())
        .filter(|text| !text.is_empty())
        .collect()
}

fn joined(sections: Vec<String>) -> Option<String> {
    let text = sections.join("\n---\n");
    (!text.is_empty()).then(|| text.chars().take(MAX_FIELD).collect())
}

/// The plan-item keys named by the writer brief.
pub(super) fn item_keys(brief: &str) -> Vec<String> {
    let found: BTreeSet<String> = regex::Regex::new(r"plangraph:[A-Za-z0-9_-]+")
        .map(|re| re.find_iter(brief).map(|m| m.as_str().to_owned()).collect())
        .unwrap_or_default();
    found.into_iter().collect()
}

/// The done criteria of every item of the brief, whole.
pub(super) fn done_criteria(brief: &str) -> Option<String> {
    joined(sections(brief, &["done criteria", "done when"]))
}

/// The invariants the item names (an `Invariants:` section, else its `How:` text).
pub(super) fn invariants(brief: &str) -> Option<String> {
    joined(sections(brief, &["invariants:", "invariant:", "how:"]))
}

/// Public symbols a unified diff adds: Rust `pub fn|struct|enum|trait|const|type` and Python
/// top-level `def`/`class` without a leading underscore.
pub(super) fn changed_symbols(diff: &str) -> Vec<String> {
    let rust = regex::Regex::new(
        r"^\+\s*pub(?:\([a-z]+\))?\s+(?:async\s+|const\s+|unsafe\s+)*(?:fn|struct|enum|trait|const|type)\s+([A-Za-z][A-Za-z0-9_]*)",
    );
    let python = regex::Regex::new(r"^\+(?:async\s+)?(?:def|class)\s+([A-Za-z][A-Za-z0-9_]*)");
    let mut found = BTreeSet::new();
    for line in diff.lines() {
        for re in [&rust, &python].into_iter().flatten() {
            if let Some(caps) = re.captures(line) {
                found.insert(caps[1].to_owned());
            }
        }
    }
    found.into_iter().collect()
}

/// `symbol: file:line, ...` for the first few uses of each symbol, from `git grep -n -w` lines.
pub(super) fn callers_line(symbol: &str, grep: &str) -> Option<String> {
    let hits: Vec<String> = grep
        .lines()
        .filter_map(|line| {
            let mut parts = line.splitn(3, ':');
            Some(format!("{}:{}", parts.next()?, parts.next()?))
        })
        .take(MAX_CALLERS_PER_SYMBOL)
        .collect();
    (!hits.is_empty()).then(|| format!("{symbol}: {}", hits.join(", ")))
}

#[cfg(test)]
#[path = "native_writer_context_tests.rs"]
mod tests;
