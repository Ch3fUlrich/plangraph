//! What each part of a train ships, read from git: the families its own commits add or change and, per
//! family, the items and `Deploy:` hosts of the commits that touch it (item-scoped attribution).
use super::deploy::{PartDeploy, Shipped};
use super::train::{Manifest, Part};
use std::collections::{BTreeMap, BTreeSet};
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

fn family_at(status: &str, path: &str) -> Option<String> {
    family_of(status, path).or_else(|| binary_of(status, path))
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

fn name_status_families(raw: &[u8]) -> BTreeSet<String> {
    let fields: Vec<String> = raw
        .split(|&b| b == 0)
        .filter(|f| !f.is_empty())
        .map(|f| String::from_utf8_lossy(f).into_owned())
        .collect();
    fields
        .chunks(2)
        .filter_map(|c| match c {
            [status, path] => family_at(status, path),
            _ => None,
        })
        .collect()
}

/// The families the net diff `from..to` ships, and the `Plan-Item` keys its commits carry: what a landed
/// train really contains, for checking a deploy plan against it.
pub(super) fn shipped_between(
    repo: &Path,
    from: &str,
    to: &str,
) -> Result<(BTreeSet<String>, BTreeSet<String>), String> {
    use super::git;
    let diff = git::bytes(
        repo,
        &[
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--name-status",
            "-z",
            from,
            to,
            "--",
        ],
    )?;
    let log = git::bytes(
        repo,
        &[
            "log",
            "--no-merges",
            "--format=%B%x00",
            &format!("{from}..{to}"),
        ],
    )?;
    let items = String::from_utf8_lossy(&log)
        .split('\0')
        .flat_map(|m| trailers(m, "Plan-Item"))
        .collect();
    Ok((name_status_families(&diff), items))
}

/// What one part ships, read from its own commits and its own diff only. A family is shipped by the net
/// diff; the items and hosts of a family come from the commits whose own change touches it (an untagged
/// follow-up counts for all the part's items), so a docs-only key of the same part inherits neither the
/// family nor its hosts.
pub fn derive_part(repo: &Path, base: &str, part: &Part) -> Result<PartDeploy, String> {
    use super::{build::part_base, git};
    let from = part_base(repo, base, part.parent.as_ref());
    let range = format!("{from}..{}", part.head);
    let shas = git::bytes(repo, &["rev-list", "--no-merges", "--reverse", &range])?;
    let (mut items, mut hosts) = (BTreeSet::new(), BTreeSet::new());
    let mut touched: BTreeMap<String, Shipped> = BTreeMap::new();
    let mut untagged_families: BTreeSet<String> = BTreeSet::new();
    for sha in String::from_utf8_lossy(&shas).split_whitespace() {
        let message = git::bytes(repo, &["show", "-s", "--format=%B", sha])?;
        let message = String::from_utf8_lossy(&message).into_owned();
        let (commit_items, commit_hosts) = (
            trailers(&message, "Plan-Item"),
            trailers(&message, "Deploy"),
        );
        items.extend(commit_items.iter().cloned());
        hosts.extend(commit_hosts.iter().cloned());
        let changed = git::bytes(
            repo,
            &[
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--no-renames",
                "--name-status",
                "-z",
                "-r",
                sha,
            ],
        )?;
        for family in name_status_families(&changed) {
            let e = touched.entry(family.clone()).or_default();
            e.items.extend(commit_items.iter().cloned());
            e.trailer_hosts.extend(commit_hosts.iter().cloned());
            if commit_items.is_empty() {
                untagged_families.insert(family);
            }
        }
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
            &part.head,
            "--",
        ],
    )?;
    let families = name_status_families(&diff);
    touched.retain(|f, _| families.contains(f));
    // The keeper's rule (`part_changes`): an untagged follow-up belongs to every item the part has own
    // work for, so a family it adds goes to all the part's items.
    for family in &untagged_families {
        if let Some(e) = touched.get_mut(family) {
            e.items.extend(items.iter().cloned());
        }
    }
    Ok(PartDeploy {
        pr: part.pr,
        families,
        items,
        trailer_hosts: hosts,
        shipped: touched,
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
