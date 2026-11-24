//! What each part of a train ships, read from the part's own commits and diff (see `deploy`).
use super::deploy::PartDeploy;
use super::train::{Manifest, Part};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::Path;

/// The family each changed path ships: a unit file, or `bin:<name>` for an added binary entry point.
pub(super) fn family_of(status: &str, path: &str) -> Option<String> {
    let rest = path.strip_prefix("deploy/systemd/")?;
    let unit = [".service", ".timer", ".socket", ".path"]
        .iter()
        .any(|s| rest.ends_with(s) && !rest.contains('/'));
    if unit && status != "D" {
        return Some(rest.to_owned());
    }
    None
}

fn binary_of(status: &str, path: &str) -> Option<String> {
    if status != "A" {
        return None;
    }
    let rest = path.strip_prefix("rust/")?;
    let (krate, tail) = rest.split_once('/')?;
    if tail == "src/main.rs" {
        return Some(format!("bin:{krate}"));
    }
    let bin = tail.strip_prefix("src/bin/")?.strip_suffix(".rs")?;
    (!bin.contains('/')).then(|| format!("bin:{bin}"))
}

fn trailers(message: &str, name: &str) -> Vec<String> {
    let want = format!("{}:", name.to_ascii_lowercase());
    message
        .lines()
        .filter(|l| l.to_ascii_lowercase().starts_with(&want))
        .flat_map(|l| l[want.len()..].split(','))
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
        .collect()
}

/// One part as a commit range: the part's own commits are `from..head`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Span {
    pub pr: u64,
    pub from: String,
    pub head: String,
}

/// What one part ships, read from its own commits and its own diff only.
pub fn derive_part(repo: &Path, base: &str, part: &Part) -> Result<PartDeploy, String> {
    use super::build::part_base;
    let from = part_base(repo, base, part.parent.as_ref());
    derive_span(
        repo,
        &Span {
            pr: part.pr,
            from: from.to_owned(),
            head: part.head.clone(),
        },
    )
}

/// What the part of one commit range ships.
pub fn derive_span(repo: &Path, span: &Span) -> Result<PartDeploy, String> {
    use super::git;
    let (from, head) = (span.from.as_str(), span.head.as_str());
    let log = git::bytes(
        repo,
        &[
            "log",
            "--no-merges",
            "--format=%B%x00",
            &format!("{from}..{head}"),
        ],
    )?;
    let (mut items, mut hosts) = (BTreeSet::new(), BTreeSet::new());
    for message in String::from_utf8_lossy(&log).split('\0') {
        items.extend(trailers(message, "Plan-Item"));
        hosts.extend(trailers(message, "Deploy"));
    }
    let diff = git::bytes(
        repo,
        &[
            "diff",
            "--merge-base",
            "--no-ext-diff",
            "--no-renames",
            "--name-status",
            "-z",
            from,
            head,
            "--",
        ],
    )?;
    let fields: Vec<String> = diff
        .split(|&b| b == 0)
        .filter(|f| !f.is_empty())
        .map(|f| String::from_utf8_lossy(f).into_owned())
        .collect();
    let families = fields
        .chunks(2)
        .filter_map(|c| match c {
            [status, path] => family_of(status, path).or_else(|| binary_of(status, path)),
            _ => None,
        })
        .collect();
    Ok(PartDeploy {
        pr: span.pr,
        families,
        items,
        trailer_hosts: hosts,
    })
}

/// Every part of the manifest, attributed on its own.
pub fn derive(repo: &Path, manifest: &Manifest) -> Result<Vec<PartDeploy>, String> {
    manifest
        .parts
        .iter()
        .map(|p| derive_part(repo, &manifest.base, p))
        .collect()
}

/// Every range of a build that is not a manifest (the batch tool knows its parts by merge commit).
pub fn derive_spans(repo: &Path, spans: &[Span]) -> Result<Vec<PartDeploy>, String> {
    spans.iter().map(|s| derive_span(repo, s)).collect()
}
