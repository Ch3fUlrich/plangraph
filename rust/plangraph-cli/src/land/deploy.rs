//! The deploy table of a train or of a landed range (TRAIN-DEPLOYS-ITS-FAMILIES).
//!
//! The families come from the train-wide landed-path attribution ([`derive_range`]): a family belongs to
//! every part whose own commits, or whose merge commit for a resolution, touch it, so a unitless part never
//! blocks on another part's unit. The hosts of a family come from the `Deploy-Hosts:` line of the items the
//! part carries, else the `Deploy:` trailers of the part's own commits, else the hosts that hold the
//! family's manifest roles; every host must be authorized. Each (unit, host) target is probed by the host
//! install dry run before the GO-REQ names it; a refused target is left out and gets a blocker item. A
//! family the manifest keeps out of train installs (not deployed, a system unit, a rendered template) is
//! listed as skipped with its reason.
use super::deploy_derive::trailers;
pub use super::deploy_derive::{derive_range, Family};
pub use super::deploy_probe::{CommandProbe, Finding, Probe};
use super::deploy_run::RunTarget;
use super::train::Manifest;
use crate::deploy_manifest::on_roles;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// What one part (or one landed commit) ships and what its commits say about where.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PartDeploy {
    pub pr: u64,
    /// Unit names and `bin:<name>` it installs; empty for a unitless part.
    pub families: BTreeSet<String>,
    /// `Plan-Item` keys of the part's own commits.
    pub items: BTreeSet<String>,
    /// Hosts of the `Deploy:` trailers of the part's own commits.
    pub trailer_hosts: BTreeSet<String>,
    /// Manifest roles of each unit in `families` (none without a manifest).
    #[serde(default)]
    pub roles: BTreeMap<String, Vec<String>>,
    /// Families it touches that a train does not install, with the reason.
    #[serde(default)]
    pub skipped: BTreeMap<String, String>,
}

impl PartDeploy {
    pub fn add(&mut self, family: &Family) {
        if let Some(why) = &family.skip {
            self.skipped.insert(family.name.clone(), why.clone());
            return;
        }
        for unit in &family.units {
            self.families.insert(unit.clone());
            self.roles.insert(unit.clone(), family.roles.clone());
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Target {
    pub family: String,
    pub host: String,
    pub parts: Vec<u64>,
    /// The items that get its deploy rows.
    #[serde(default)]
    pub items: BTreeSet<String>,
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
    /// Families left to the operator, with the reason.
    #[serde(default)]
    pub skipped: BTreeMap<String, String>,
}

impl DeployPlan {
    /// `deploy: unit@host, ...` for the GO-REQ; `deploy: none` when no part ships a family.
    pub fn go_req(&self) -> String {
        if self.targets.is_empty() {
            return "deploy: none".into();
        }
        let names: Vec<_> = self
            .targets
            .iter()
            .map(|t| format!("{}@{}", t.family, t.host))
            .collect();
        format!("deploy: {}", names.join(", "))
    }

    /// What `deploy-apply` installs.
    pub fn run_targets(&self) -> Vec<RunTarget> {
        self.targets
            .iter()
            .map(|t| RunTarget {
                family: t.family.clone(),
                host: t.host.clone(),
                items: t.items.clone(),
            })
            .collect()
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

/// What each part of a built train ships. The families come from the train-wide attribution of
/// `base..train_sha`: a family belongs to every part whose own commits (or whose merge commit, for a
/// resolution) touch it; a family no part's commit names goes to the last part. The items and hosts are
/// those of the part's own commits.
pub fn derive(repo: &Path, manifest: &Manifest) -> Result<Vec<PartDeploy>, String> {
    use super::{build::part_base, git};
    let derived = derive_range(repo, &manifest.base, &manifest.train_sha)?;
    let mut claimed = BTreeSet::new();
    let mut out = Vec::new();
    for (i, part) in manifest.parts.iter().enumerate() {
        let from = part_base(repo, &manifest.base, part.parent.as_ref());
        let range = format!("{from}..{}", part.head);
        let log = git::bytes(repo, &["log", "--no-merges", "--format=%B%x00", &range])?;
        let mut pd = PartDeploy {
            pr: part.pr,
            ..PartDeploy::default()
        };
        for message in String::from_utf8_lossy(&log).split('\0') {
            pd.items.extend(trailers(message, "Plan-Item"));
            pd.trailer_hosts.extend(trailers(message, "Deploy"));
        }
        let mut own: BTreeSet<String> = git::commits(repo, &range)?.into_iter().collect();
        own.extend(manifest.merges.get(i).cloned());
        let last = i + 1 == manifest.parts.len();
        for touch in &derived.touches {
            if own.contains(&touch.sha) || (last && !claimed.contains(&touch.sha)) {
                claimed.insert(touch.sha.clone());
                for family in touch
                    .families
                    .iter()
                    .filter_map(|f| derived.families.get(f))
                {
                    pd.add(family);
                }
            }
        }
        out.push(pd);
    }
    Ok(out)
}

/// What a landed range ships, one entry per commit that touches a family (part number 0): its items and
/// `Deploy:` hosts, and the families its paths ship train-wide.
pub fn derive_landed(repo: &Path, base: &str, head: &str) -> Result<Vec<PartDeploy>, String> {
    let derived = derive_range(repo, base, head)?;
    Ok(derived
        .touches
        .iter()
        .map(|touch| {
            let mut pd = PartDeploy {
                items: touch.items.clone(),
                trailer_hosts: touch.hosts.clone(),
                ..PartDeploy::default()
            };
            for family in touch
                .families
                .iter()
                .filter_map(|f| derived.families.get(f))
            {
                pd.add(family);
            }
            pd
        })
        .collect())
}

/// The manifest roles each host holds.
pub type RoleHosts = BTreeMap<String, BTreeSet<String>>;

/// The hosts a part may deploy `unit` to: item-level `Deploy-Hosts:` first, else its commits' `Deploy:`
/// trailers, else the hosts holding one of the unit's manifest roles. A host outside `authorized` is
/// returned in the second list.
fn hosts_of(
    part: &PartDeploy,
    unit: &str,
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
    role_hosts: &RoleHosts,
) -> (BTreeSet<String>, BTreeSet<String>) {
    let item_level: BTreeSet<String> = part
        .items
        .iter()
        .filter_map(|k| declared.get(k))
        .flatten()
        .cloned()
        .collect();
    let named = if !item_level.is_empty() {
        item_level
    } else if !part.trailer_hosts.is_empty() {
        part.trailer_hosts.clone()
    } else {
        let roles = part.roles.get(unit).cloned().unwrap_or_default();
        role_hosts
            .iter()
            .filter(|(_, held)| on_roles(&roles, held))
            .map(|(h, _)| h.clone())
            .collect()
    };
    named.into_iter().partition(|h| authorized.contains(h))
}

/// The plan of parts whose hosts come from declarations and trailers only.
pub fn deploy_plan(
    parts: &[PartDeploy],
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
    probe: &dyn Probe,
) -> DeployPlan {
    plan_with_roles(parts, declared, authorized, &RoleHosts::new(), probe)
}

/// The plan: probe each (unit, host) of each part that ships one. Unitless parts contribute nothing.
pub fn plan_with_roles(
    parts: &[PartDeploy],
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
    role_hosts: &RoleHosts,
    probe: &dyn Probe,
) -> DeployPlan {
    let mut ok: BTreeMap<(String, String), (Vec<u64>, BTreeSet<String>)> = BTreeMap::new();
    let mut bad: BTreeMap<BlockerKey, (Vec<u64>, Vec<String>)> = BTreeMap::new();
    let mut skipped = BTreeMap::new();
    let mut refuse = |family: &str, host: Option<&str>, pr: u64, why: String| {
        let e = bad
            .entry((family.to_owned(), host.map(str::to_owned)))
            .or_default();
        e.0.push(pr);
        e.1.push(why);
    };
    for part in parts {
        skipped.extend(part.skipped.clone());
        for family in &part.families {
            let (allowed, denied) = hosts_of(part, family, declared, authorized, role_hosts);
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
                    "no Deploy-Hosts, Deploy trailer or host role names a host".into(),
                );
            }
            for host in &allowed {
                match probe.probe(host, family) {
                    Ok(()) => {
                        let e = ok.entry((family.clone(), host.clone())).or_default();
                        e.0.push(part.pr);
                        e.1.extend(part.items.iter().cloned());
                    }
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
        targets: ok
            .into_iter()
            .map(|((family, host), (mut parts, items))| {
                parts.dedup();
                Target {
                    family,
                    host,
                    parts,
                    items,
                }
            })
            .collect(),
        blockers: bad
            .into_iter()
            .map(|((family, host), (mut parts, mut reasons))| {
                parts.dedup();
                reasons.dedup();
                Blocker {
                    key: blocker_key(&family, host.as_deref()),
                    family,
                    host,
                    parts,
                    reasons,
                }
            })
            .collect(),
        skipped,
    }
}

/// A probe that passes every target: the plan without the host install dry run.
pub struct NoProbe;

impl Probe for NoProbe {
    fn probe(&self, _host: &str, _family: &str) -> Result<(), Vec<Finding>> {
        Ok(())
    }
}

/// `host=role+role,...` (`keeper-host=keeper,build-host=worker+wsl`).
pub fn parse_role_hosts(spec: &str) -> Result<RoleHosts, String> {
    spec.split(',')
        .filter(|p| !p.trim().is_empty())
        .map(|pair| {
            let (host, roles) = pair
                .split_once('=')
                .ok_or_else(|| format!("role host {pair:?} needs host=roles"))?;
            let roles = crate::deploy_manifest::host_roles(Some(&roles.replace('+', ",")), None)?;
            Ok((host.trim().to_owned(), roles))
        })
        .collect()
}
