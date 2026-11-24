//! The deploy table of a train, derived per part (TRAIN-DEPLOYS-ITS-FAMILIES).
//!
//! A part ships a family when its own commits add or change a systemd unit under `deploy/systemd` or add a
//! host binary; the family belongs to that part alone, so a unitless part never blocks on another part's
//! unit. The hosts of a family come from the `Deploy-Hosts:` line of the items the part carries, else the
//! `Deploy:` trailers of the part's own commits, and every host must be authorized. Each (family, host)
//! target is probed by the host install dry run before the GO-REQ names it; a refused target is left out
//! and gets a blocker item.
pub use super::deploy_derive::{derive, derive_part};
pub use super::deploy_probe::{CommandProbe, Finding, Probe};
use super::train::Manifest;
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
    /// Per family: the items and `Deploy:` hosts of the commits that touch it. A family without an entry
    /// falls back to the part's `items` and `trailer_hosts`.
    #[serde(default)]
    pub shipped: BTreeMap<String, Shipped>,
}

/// The items and `Deploy:` trailer hosts of the commits that ship one family.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Shipped {
    pub items: BTreeSet<String>,
    pub trailer_hosts: BTreeSet<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Target {
    pub family: String,
    pub host: String,
    pub parts: Vec<u64>,
    /// `Plan-Item` keys of the parts that ship this family: the items that get its deploy row.
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
    /// The built train this plan belongs to (the manifest's `train_sha`) and its base; `deploy-apply`
    /// checks the plan against what that train really changed.
    #[serde(default)]
    pub train_sha: String,
    #[serde(default)]
    pub base: String,
}

impl DeployPlan {
    /// SHA-256 of the whole plan (train, base, targets with their item keys, blockers). The approved GO
    /// carries it, so a plan edited after approval no longer matches.
    pub fn digest(&self) -> String {
        use sha2::{Digest, Sha256};
        let text = serde_json::to_vec(self).unwrap_or_default();
        format!("{:x}", Sha256::digest(text))
    }

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

/// The hosts of one family, per item: item-level `Deploy-Hosts:` first, else the `Deploy:` trailers of the
/// commits that ship the family. Returns authorized host -> the items that name it, and the unauthorized
/// hosts. A family with no item is placed by the trailers alone.
fn hosts_of(
    items: &BTreeSet<String>,
    trailer_hosts: &BTreeSet<String>,
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
) -> (BTreeMap<String, BTreeSet<String>>, BTreeSet<String>) {
    let (mut allowed, mut denied): (BTreeMap<String, BTreeSet<String>>, BTreeSet<String>) =
        Default::default();
    let mut place = |named: &BTreeSet<String>, item: Option<&String>| {
        for host in named {
            if authorized.contains(host) {
                let e = allowed.entry(host.clone()).or_default();
                e.extend(item.cloned());
            } else {
                denied.insert(host.clone());
            }
        }
    };
    if items.is_empty() {
        place(trailer_hosts, None);
    }
    for item in items {
        let item_level: BTreeSet<String> =
            declared.get(item).into_iter().flatten().cloned().collect();
        place(
            if item_level.is_empty() {
                trailer_hosts
            } else {
                &item_level
            },
            Some(item),
        );
    }
    (allowed, denied)
}

/// The plan: probe each (family, host) of each part that ships one. Unitless parts contribute nothing.
pub fn deploy_plan(
    parts: &[PartDeploy],
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
    probe: &dyn Probe,
) -> DeployPlan {
    let mut ok: BTreeMap<(String, String), (Vec<u64>, BTreeSet<String>)> = BTreeMap::new();
    let mut bad: BTreeMap<BlockerKey, (Vec<u64>, Vec<String>)> = BTreeMap::new();
    let mut refuse = |family: &str, host: Option<&str>, pr: u64, why: String| {
        let e = bad
            .entry((family.to_owned(), host.map(str::to_owned)))
            .or_default();
        e.0.push(pr);
        e.1.push(why);
    };
    for part in parts.iter().filter(|p| !p.families.is_empty()) {
        for family in &part.families {
            let (items, trailer_hosts) = match part.shipped.get(family) {
                Some(s) => (&s.items, &s.trailer_hosts),
                None => (&part.items, &part.trailer_hosts),
            };
            let (allowed, denied) = hosts_of(items, trailer_hosts, declared, authorized);
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
            for (host, host_items) in &allowed {
                match probe.probe(host, family) {
                    Ok(()) => {
                        let e = ok.entry((family.clone(), host.clone())).or_default();
                        e.0.push(part.pr);
                        e.1.extend(host_items.iter().cloned());
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
            .map(|((family, host), (parts, items))| Target {
                family,
                host,
                parts,
                items,
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
        train_sha: String::new(),
        base: String::new(),
    }
}

/// `land-train deploy-plan --repo DIR --manifest FILE --authorized-hosts A,B --probe-cmd "CMD ARGS"
/// [--declared FILE] [--blockers-out FILE] [--plan-out FILE]`: the deploy table of a built train. `--declared` is a JSON
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
    plan.train_sha = manifest.train_sha.clone();
    plan.base = manifest.base.clone();
    if let Some(out) = flags.get("plan-out") {
        let text = serde_json::to_vec_pretty(&plan).map_err(|e| e.to_string())?;
        std::fs::write(out, text).map_err(|e| format!("{out}: {e}"))?;
    }
    if let Some(out) = flags.get("blockers-out") {
        let text = serde_json::to_vec_pretty(&plan.blocker_items()).map_err(|e| e.to_string())?;
        std::fs::write(out, text).map_err(|e| format!("{out}: {e}"))?;
    }
    let mut lines = vec![plan.go_req(), format!("plan-digest: {}", plan.digest())];
    lines.extend(
        plan.blockers
            .iter()
            .map(|b| format!("blocker {}: {}", b.key, b.reasons.join("; "))),
    );
    Ok(lines)
}
