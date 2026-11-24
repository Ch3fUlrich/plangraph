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

/// The families of a net `--name-status -z` diff, each with the paths that ship it.
fn net_families(raw: &[u8]) -> BTreeMap<String, BTreeSet<String>> {
    let fields: Vec<String> = raw
        .split(|&b| b == 0)
        .filter(|f| !f.is_empty())
        .map(|f| String::from_utf8_lossy(f).into_owned())
        .collect();
    let mut out: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for c in fields.chunks(2) {
        if let [status, path] = c {
            if let Some(f) = family_at(status, path) {
                out.entry(f).or_default().insert(path.clone());
            }
        }
    }
    out
}

/// The families the net diff `from..to` ships, each with its paths.
pub(super) fn families_between(
    repo: &Path,
    from: &str,
    to: &str,
) -> Result<BTreeMap<String, BTreeSet<String>>, String> {
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
    Ok(net_families(&diff))
}

/// What the commits of a range touch, attributed to items: per net family the items and `Deploy:` hosts of
/// the commits that touch one of its paths (whatever their per-commit status: an untagged edit of a binary
/// added earlier still counts), the families touched by a commit without `Plan-Item`, and every item and
/// host of the range.
#[derive(Default)]
pub(super) struct Touched {
    pub by_family: BTreeMap<String, Shipped>,
    pub untagged: BTreeSet<String>,
    pub items: BTreeSet<String>,
    pub hosts: BTreeSet<String>,
}

pub(super) fn touched_in(
    repo: &Path,
    range: &str,
    net: &BTreeMap<String, BTreeSet<String>>,
) -> Result<Touched, String> {
    use super::git;
    let shas = git::bytes(repo, &["rev-list", "--no-merges", "--reverse", range])?;
    let mut out = Touched::default();
    for sha in String::from_utf8_lossy(&shas).split_whitespace() {
        let message = git::bytes(repo, &["show", "-s", "--format=%B", sha])?;
        let message = String::from_utf8_lossy(&message).into_owned();
        let (commit_items, commit_hosts) = (
            trailers(&message, "Plan-Item"),
            trailers(&message, "Deploy"),
        );
        out.items.extend(commit_items.iter().cloned());
        out.hosts.extend(commit_hosts.iter().cloned());
        let changed = git::bytes(
            repo,
            &[
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--no-renames",
                "--name-only",
                "-z",
                "-r",
                sha,
            ],
        )?;
        let paths: BTreeSet<String> = changed
            .split(|&b| b == 0)
            .filter(|p| !p.is_empty())
            .map(|p| String::from_utf8_lossy(p).into_owned())
            .collect();
        for (family, family_paths) in net {
            if family_paths.is_disjoint(&paths) {
                continue;
            }
            let e = out.by_family.entry(family.clone()).or_default();
            e.items.extend(commit_items.iter().cloned());
            e.trailer_hosts.extend(commit_hosts.iter().cloned());
            if commit_items.is_empty() {
                out.untagged.insert(family.clone());
            }
        }
    }
    Ok(out)
}

/// What one part ships, read from its own commits and its own diff only. A family is shipped by the net
/// diff; the items and hosts of a family come from the commits that touch it (an untagged follow-up counts
/// for all the part's items), so a docs-only key of the same part inherits neither the family nor its hosts.
pub fn derive_part(repo: &Path, base: &str, part: &Part) -> Result<PartDeploy, String> {
    use super::build::part_base;
    let from = part_base(repo, base, part.parent.as_ref());
    let net = families_between_merge_base(repo, from, &part.head)?;
    let touched = touched_in(repo, &format!("{from}..{}", part.head), &net)?;
    let mut shipped = touched.by_family;
    // The keeper's rule (`part_changes`): an untagged follow-up belongs to every item the part has own
    // work for, so a family it touches goes to all the part's items.
    for family in &touched.untagged {
        if let Some(e) = shipped.get_mut(family) {
            e.items.extend(touched.items.iter().cloned());
        }
    }
    Ok(PartDeploy {
        pr: part.pr,
        families: net.into_keys().collect(),
        items: touched.items,
        trailer_hosts: touched.hosts,
        shipped,
    })
}

fn families_between_merge_base(
    repo: &Path,
    from: &str,
    head: &str,
) -> Result<BTreeMap<String, BTreeSet<String>>, String> {
    use super::git;
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
    Ok(net_families(&diff))
}

/// Every part of the manifest, attributed on its own, then reduced to what the whole train nets out to: a
/// unit one part adds and a later part removes is no deploy target.
pub fn derive(repo: &Path, manifest: &Manifest) -> Result<Vec<PartDeploy>, String> {
    let net = families_between(repo, &manifest.base, &manifest.train_sha)?;
    let mut parts: Vec<PartDeploy> = manifest
        .parts
        .iter()
        .map(|p| derive_part(repo, &manifest.base, p))
        .collect::<Result<_, _>>()?;
    for p in &mut parts {
        p.families.retain(|f| net.contains_key(f));
        p.shipped.retain(|f, _| net.contains_key(f));
    }
    Ok(parts)
}
