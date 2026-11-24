//! The deploy table of a train, derived per part (TRAIN-DEPLOYS-ITS-FAMILIES).
//!
//! A part ships a family when its own commits add or change a systemd unit under `deploy/systemd` or add a
//! host binary; the family belongs to that part alone, so a unitless part never blocks on another part's
//! unit. The hosts of a family come from the `Deploy-Hosts:` line of the items the part carries, else the
//! `Deploy:` trailers of the part's own commits, and every host must be authorized. Each (family, host)
//! target is probed by the host install dry run before the GO-REQ names it; a refused target is left out
//! and gets a blocker item.
pub use super::deploy_probe::{CommandProbe, Finding, Probe};
use super::train::{Manifest, Part};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// What one part ships and what its commits say about where.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PartDeploy {
    pub pr: u64,
    /// `<unit>` file names and `bin:<name>` of binaries this part adds or changes; empty for a unitless part.
    pub families: BTreeSet<String>,
    /// `Plan-Item` keys of the part's own commits.
    pub items: BTreeSet<String>,
    /// Hosts of the `Deploy:` trailers of the part's own commits.
    pub trailer_hosts: BTreeSet<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Target {
    pub family: String,
    pub host: String,
    pub parts: Vec<u64>,
}

/// A refused target (or a family without a usable host): the item that tracks it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Blocker {
    pub key: String,
    pub family: String,
    /// `None` when the family names no authorized host at all.
    pub host: Option<String>,
    pub parts: Vec<u64>,
    pub reasons: Vec<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeployPlan {
    /// Probed and passed: exactly what the GO-REQ names.
    pub targets: Vec<Target>,
    pub blockers: Vec<Blocker>,
    /// The keeper schema version the train raises to; the GO needs `MIGRATION <it>`.
    #[serde(default)]
    pub keeper_migration: Option<u32>,
}

impl DeployPlan {
    /// `deploy: unit@host, ...` for the GO-REQ; `deploy: none` when no part ships a family. A train that
    /// raises the keeper schema adds the standalone `MIGRATION <version>` field and names `keeper-restart`.
    pub fn go_req(&self) -> String {
        let names: Vec<_> = self
            .targets
            .iter()
            .map(|t| format!("{}@{}", t.family, t.host))
            .collect();
        let units = if names.is_empty() {
            "deploy: none".to_owned()
        } else {
            format!("deploy: {}", names.join(", "))
        };
        match self.keeper_migration {
            Some(version) => format!(
                "{units}; MIGRATION {version}; keeper deploy: plangraph keeper-restart CHECKOUT KEEPER_CONFIG MIGRATION_DSN_FILE, never a plain restart"
            ),
            None => units,
        }
    }

    /// One keeper definition per blocker, in `pg define --file` format.
    pub fn blocker_items(&self) -> serde_json::Value {
        let items: Vec<_> = self
            .blockers
            .iter()
            .map(|b| {
                let at = b.host.as_deref().map_or(String::new(), |h| format!("@{h}"));
                let title = format!("Train deploy target {}{at} refused: {}", b.family, b.reasons.join("; "));
                let parts: Vec<_> = b.parts.iter().map(|p| format!("#{p}")).collect();
                serde_json::json!({
                    "key": b.key, "project": "plangraph", "kind": "task",
                    "under": "plangraph:LESSONS-ENFORCE", "priority": 10,
                    "title": title.chars().take(200).collect::<String>(),
                    "description": format!(
                        "Why: the train deploy preflight refused {}{at} of part(s) {}: {}.\nHow: fix the host or the Deploy-Hosts/Deploy declaration, rerun the dry run.\nDone when: the dry run passes for this target.",
                        b.family, parts.join(", "), b.reasons.join("; ")),
                })
            })
            .collect();
        serde_json::json!({ "items": items })
    }
}

type BlockerKey = (String, Option<String>);

fn blocker_key(family: &str, host: Option<&str>) -> String {
    let raw = format!("{family}-{}", host.unwrap_or("nohost"));
    let clean: String = raw
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_uppercase()
            } else {
                '-'
            }
        })
        .collect();
    format!("plangraph:LE-DEPLOY-BLOCKED-{}", clean.trim_matches('-'))
}

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

/// What one part ships, read from its own commits and its own diff only.
pub fn derive_part(repo: &Path, base: &str, part: &Part) -> Result<PartDeploy, String> {
    use super::{build::part_base, git};
    let from = part_base(repo, base, part.parent.as_ref());
    let log = git::bytes(
        repo,
        &[
            "log",
            "--no-merges",
            "--format=%B%x00",
            &format!("{from}..{}", part.head),
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
            &part.head,
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
        pr: part.pr,
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

/// The hosts a part may deploy to: item-level `Deploy-Hosts:` first, else its commits' `Deploy:` trailers.
/// A host outside `authorized` is returned in the second list.
fn hosts_of(
    part: &PartDeploy,
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
) -> (BTreeSet<String>, BTreeSet<String>) {
    let item_level: BTreeSet<String> = part
        .items
        .iter()
        .filter_map(|k| declared.get(k))
        .flatten()
        .cloned()
        .collect();
    let named = if item_level.is_empty() {
        part.trailer_hosts.clone()
    } else {
        item_level
    };
    named.into_iter().partition(|h| authorized.contains(h))
}

/// The keeper schema version the whole train raises to: the manifest base against the final train commit,
/// so a part branched from an older main never reads as a regression and a migration brought in by an
/// adopted resolution is not missed.
pub fn train_migration(repo: &Path, manifest: &Manifest) -> Result<Option<u32>, String> {
    super::keeper_schema::rise(repo, &manifest.base, &manifest.train_sha)
}

/// The plan: probe each (family, host) of each part that ships one. Unitless parts contribute nothing.
pub fn deploy_plan(
    parts: &[PartDeploy],
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
    probe: &dyn Probe,
) -> DeployPlan {
    let mut ok: BTreeMap<(String, String), Vec<u64>> = BTreeMap::new();
    let mut bad: BTreeMap<BlockerKey, (Vec<u64>, Vec<String>)> = BTreeMap::new();
    let mut refuse = |family: &str, host: Option<&str>, pr: u64, why: String| {
        let e = bad
            .entry((family.to_owned(), host.map(str::to_owned)))
            .or_default();
        e.0.push(pr);
        e.1.push(why);
    };
    for part in parts.iter().filter(|p| !p.families.is_empty()) {
        let (allowed, denied) = hosts_of(part, declared, authorized);
        for family in &part.families {
            for host in &denied {
                refuse(
                    family,
                    Some(host),
                    part.pr,
                    format!("host {host} is not authorized"),
                );
            }
            if allowed.is_empty() && denied.is_empty() {
                refuse(
                    family,
                    None,
                    part.pr,
                    "no Deploy-Hosts or Deploy trailer names a host".into(),
                );
            }
            for host in &allowed {
                match probe.probe(host, family) {
                    Ok(()) => ok
                        .entry((family.clone(), host.clone()))
                        .or_default()
                        .push(part.pr),
                    Err(found) => {
                        let why: Vec<_> = found
                            .iter()
                            .map(|f| format!("missing {} {}", f.kind, f.name))
                            .collect();
                        refuse(family, Some(host), part.pr, why.join(", "));
                    }
                }
            }
        }
    }
    DeployPlan {
        keeper_migration: None,
        targets: ok
            .into_iter()
            .map(|((family, host), parts)| Target {
                family,
                host,
                parts,
            })
            .collect(),
        blockers: bad
            .into_iter()
            .map(|((family, host), (parts, reasons))| Blocker {
                key: blocker_key(&family, host.as_deref()),
                family,
                host,
                parts,
                reasons,
            })
            .collect(),
    }
}

/// `land-train deploy-plan --repo DIR --manifest FILE --authorized-hosts A,B --probe-cmd "CMD ARGS"
/// [--declared FILE] [--blockers-out FILE]`: the deploy table of a built train. `--declared` is a JSON
/// object of item key -> hosts from the items' `Deploy-Hosts:` lines.
pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let read = |p: &str| std::fs::read(p).map_err(|e| format!("{p}: {e}"));
    let manifest: Manifest =
        serde_json::from_slice(&read(need("manifest")?)?).map_err(|e| e.to_string())?;
    let declared: BTreeMap<String, Vec<String>> = match flags.get("declared") {
        Some(p) => serde_json::from_slice(&read(p)?).map_err(|e| format!("{p}: {e}"))?,
        None => BTreeMap::new(),
    };
    let authorized = need("authorized-hosts")?
        .split(',')
        .filter(|h| !h.is_empty())
        .map(str::to_owned)
        .collect();
    let probe = CommandProbe {
        argv: need("probe-cmd")?
            .split_whitespace()
            .map(str::to_owned)
            .collect(),
    };
    let parts = derive(Path::new(need("repo")?), &manifest)?;
    let mut plan = deploy_plan(&parts, &declared, &authorized, &probe);
    plan.keeper_migration = train_migration(Path::new(need("repo")?), &manifest)?;
    if let Some(out) = flags.get("blockers-out") {
        let text = serde_json::to_vec_pretty(&plan.blocker_items()).map_err(|e| e.to_string())?;
        std::fs::write(out, text).map_err(|e| format!("{out}: {e}"))?;
    }
    let mut lines = vec![plan.go_req()];
    lines.extend(
        plan.blockers
            .iter()
            .map(|b| format!("blocker {}: {}", b.key, b.reasons.join("; "))),
    );
    Ok(lines)
}
