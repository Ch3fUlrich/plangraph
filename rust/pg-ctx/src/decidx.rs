//! Hermetic decisions-log parsing; git provenance and CLI wiring are separate slices.
use regex::Regex;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Write};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub id: String,
    pub text: String,
    pub kind: String,
    pub status: String,
    pub text_hash: String,
}

fn normalize(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn id_key(id: &str) -> (usize, &str, &str) {
    let digits = id.trim_start_matches("D-").trim_start_matches('0');
    (digits.len(), digits, id)
}

// Markdown backslash-escaped pipes belong to a cell, not the table structure.
fn cells(line: &str) -> Vec<String> {
    let mut result = Vec::new();
    let mut start = 0;
    let mut escaped = false;
    for (i, ch) in line.char_indices() {
        if ch == '|' && !escaped {
            result.push(normalize(&line[start..i]));
            start = i + 1;
        }
        escaped = ch == '\\' && !escaped;
    }
    result.push(normalize(&line[start..]));
    result
}

/// `rules` pairs rule names with their text; D-id references use whole-token matching.
/// `operator_marker` is the resume-digest author marker (case-insensitive); empty disables it.
pub fn parse(log: &str, rules: &[(&str, &str)], operator_marker: &str) -> Result<Vec<Row>, String> {
    let id = Regex::new(r"\bD-[0-9]+\b").unwrap();
    let exact_id = Regex::new(r"^D-[0-9]+$").unwrap();
    let wording =
        Regex::new(r"(?i)\b(?:withdraw\w*|withdrew|supersed\w*|lift(?:s|ed|ing)?)\b").unwrap();
    let active = Regex::new(r"(?i)\b(?:withdraws?|withdrew|supersed(?:e[sd]?|ing)|lift(?:s|ed|ing)?)\s+((?:D-[0-9]+)(?:\s*(?:,|and|/)\s*D-[0-9]+)*)\b").unwrap();
    let passive = Regex::new(r"(?i)(?:(D-[0-9]+)\s+(?:GO\s+|HOLD\s+)?)?\b(?:withdrawn|superseded|lifted)\b(?:\s+by\s+(D-[0-9]+))?").unwrap();
    let mut rows = BTreeMap::new();
    let mut unclear = BTreeSet::new();
    let mut edges: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let marker = normalize(operator_marker).to_lowercase();
    for line in log.lines().map(str::trim) {
        let Some(body) = line.strip_prefix('|') else {
            continue;
        };
        let fields = cells(body.strip_suffix('|').unwrap_or(body));
        if !exact_id.is_match(&fields[0]) {
            continue;
        }
        let key = fields[0].clone();
        if rows.contains_key(&key) {
            return Err(format!("duplicate D-id: {key}"));
        }
        let text = fields[1..].join(" | ");
        let author = fields.get(3).map(|s| s.to_lowercase()).unwrap_or_default();
        let kind = if !marker.is_empty() && text.to_lowercase().contains(&marker) {
            "operator"
        } else if author
            .split(|c: char| !c.is_alphanumeric() && c != '-')
            .any(|s| s == "router" || s.starts_with("routing-"))
        {
            "router"
        } else {
            "mechanical"
        };
        if fields.len() != 5 || fields[2].is_empty() {
            unclear.insert(key.clone());
        } else {
            let decision = &fields[2];
            let mut found = false;
            let mut edge = |old: &str, new: &str| {
                if old != new {
                    edges
                        .entry(old.to_owned())
                        .or_default()
                        .insert(new.to_owned());
                    found = true;
                }
            };
            for capture in active.captures_iter(decision) {
                for target in id.find_iter(&capture[1]) {
                    edge(target.as_str(), &key);
                }
            }
            for capture in passive.captures_iter(decision) {
                let old = capture.get(1).map_or(key.as_str(), |m| m.as_str());
                let new = capture.get(2).map_or(key.as_str(), |m| m.as_str());
                edge(old, new);
            }
            if wording.is_match(decision) && !found {
                unclear.insert(key.clone());
            }
        }
        rows.insert(
            key.clone(),
            Row {
                id: key,
                text_hash: format!("{:x}", Sha256::digest(text.as_bytes())),
                text,
                kind: kind.into(),
                status: "history".into(),
            },
        );
    }
    for row in rows.values_mut() {
        row.status = if unclear.contains(&row.id) {
            "unknown".into()
        } else if let Some(targets) = edges.get(&row.id) {
            if targets.len() == 1 {
                format!("superseded_by {}", targets.first().unwrap())
            } else {
                "unknown".into()
            }
        } else {
            let names: BTreeSet<_> = rules
                .iter()
                .filter(|(_, text)| id.find_iter(text).any(|m| m.as_str() == row.id))
                .map(|(name, _)| normalize(name))
                .collect();
            if names.is_empty() {
                "history".into()
            } else {
                format!(
                    "in_force {}",
                    names.into_iter().collect::<Vec<_>>().join(",")
                )
            }
        };
    }
    let mut result: Vec<_> = rows.into_values().collect();
    result.sort_by(|a, b| id_key(&a.id).cmp(&id_key(&b.id)));
    Ok(result)
}

/// Fixed four-column schema, numeric D-id order, UTF-8 and LF on every platform.
pub fn write_tsv(rows: &[Row], mut writer: impl Write) -> io::Result<()> {
    let mut sorted: Vec<_> = rows.iter().collect();
    sorted.sort_by(|a, b| id_key(&a.id).cmp(&id_key(&b.id)));
    writeln!(writer, "d_id\tkind\tstatus\ttext_hash")?;
    for row in sorted {
        writeln!(
            writer,
            "{}\t{}\t{}\t{}",
            row.id, row.kind, row.status, row.text_hash
        )?;
    }
    Ok(())
}
