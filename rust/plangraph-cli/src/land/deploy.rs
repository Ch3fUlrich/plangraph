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

    /// The lines of `deploy-plan`: the GO-REQ line, then one block per family and host that routing can quote:
    /// `READY family@host parts #N` alone, or `NOT READY family@host parts #N blocker KEY` followed by one
    /// `MISSING kind name` (or `REFUSED why`) line per reason.
    pub fn report(&self) -> Vec<String> {
        let parts = |p: &[u64]| {
            let list: Vec<_> = p.iter().map(|n| format!("#{n}")).collect();
            list.join(", ")
        };
        let mut lines = vec![self.go_req()];
        for t in &self.targets {
            lines.push(format!(
                "READY {}@{} parts {}",
                t.family,
                t.host,
                parts(&t.parts)
            ));
        }
        for b in &self.blockers {
            let host = b.host.as_deref().unwrap_or("-");
            lines.push(format!(
                "NOT READY {}@{host} parts {} blocker {}",
                b.family,
                parts(&b.parts),
                b.key
            ));
            lines.extend(b.reasons.iter().map(|r| match r.strip_prefix("missing ") {
                Some(thing) => format!("MISSING {thing}"),
                None => format!("REFUSED {r}"),
            }));
        }
        lines
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

/// A dry-run finding as a blocker reason; `report` prints `missing ...` reasons as `MISSING ...` lines.
fn finding_reason(f: &Finding) -> String {
    match f.kind.as_str() {
        "dry-run" => format!("dry run refused: {}", f.name),
        kind => format!("missing {kind} {}", f.name),
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

/// The plan: probe each (family, host) of each part that ships one. Unitless parts contribute nothing.
pub fn deploy_plan(
    parts: &[PartDeploy],
    declared: &BTreeMap<String, Vec<String>>,
    authorized: &BTreeSet<String>,
    probe: &dyn Probe,
) -> DeployPlan {
    let mut ok: BTreeMap<(String, String), Vec<u64>> = BTreeMap::new();
    let mut bad: BTreeMap<BlockerKey, (Vec<u64>, Vec<String>)> = BTreeMap::new();
    let mut refuse = |family: &str, host: Option<&str>, pr: u64, why: Vec<String>| {
        let e = bad
            .entry((family.to_owned(), host.map(str::to_owned)))
            .or_default();
        if !e.0.contains(&pr) {
            e.0.push(pr);
        }
        for reason in why {
            if !e.1.contains(&reason) {
                e.1.push(reason);
            }
        }
    };
    for part in parts.iter().filter(|p| !p.families.is_empty()) {
        let (allowed, denied) = hosts_of(part, declared, authorized);
        for family in &part.families {
            for host in &denied {
                refuse(
                    family,
                    Some(host),
                    part.pr,
                    vec![format!("host {host} is not authorized")],
                );
            }
            if allowed.is_empty() && denied.is_empty() {
                refuse(
                    family,
                    None,
                    part.pr,
                    vec!["no Deploy-Hosts or Deploy trailer names a host".into()],
                );
            }
            for host in &allowed {
                match probe.probe(host, family) {
                    Ok(()) => ok
                        .entry((family.clone(), host.clone()))
                        .or_default()
                        .push(part.pr),
                    Err(found) => {
                        let why = found.iter().map(finding_reason).collect();
                        refuse(family, Some(host), part.pr, why);
                    }
                }
            }
        }
    }
    DeployPlan {
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

/// `land-train deploy-plan --repo DIR (--manifest FILE | --spans FILE) (--authorized-hosts A,B | --registry FILE)
/// --probe-cmd "CMD ARGS" [--declared FILE] [--blockers-out FILE]`: the deploy table of a built train.
/// `--spans` is a JSON array of `{pr, from, head}` (a build the batch tool journaled without a manifest);
/// `--registry` authorizes every host id and alias of the hosts registry; `--declared` is a JSON object of
/// item key -> hosts from the items' `Deploy-Hosts:` lines. The lines are `DeployPlan::report`.
pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let read = |p: &str| std::fs::read(p).map_err(|e| format!("{p}: {e}"));
    let declared: BTreeMap<String, Vec<String>> = match flags.get("declared") {
        Some(p) => serde_json::from_slice(&read(p)?).map_err(|e| format!("{p}: {e}"))?,
        None => BTreeMap::new(),
    };
    let mut authorized: BTreeSet<String> = flags
        .get("authorized-hosts")
        .map(|list| {
            list.split(',')
                .filter(|h| !h.is_empty())
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    if let Some(path) = flags.get("registry") {
        let registry = pg_worker_core::hosts::HostRegistry::load(Path::new(path))
            .map_err(|e| format!("{path}: {e}"))?;
        for host in &registry.hosts {
            authorized.insert(host.id.clone());
            authorized.extend(host.aliases.iter().cloned());
        }
    }
    if authorized.is_empty() {
        return Err("missing --authorized-hosts or --registry".into());
    }
    let probe = CommandProbe {
        argv: need("probe-cmd")?
            .split_whitespace()
            .map(str::to_owned)
            .collect(),
    };
    let repo = Path::new(need("repo")?);
    let parts = match (flags.get("manifest"), flags.get("spans")) {
        (Some(path), None) => {
            let manifest: Manifest =
                serde_json::from_slice(&read(path)?).map_err(|e| format!("{path}: {e}"))?;
            derive(repo, &manifest)?
        }
        (None, Some(path)) => {
            let spans: Vec<Span> =
                serde_json::from_slice(&read(path)?).map_err(|e| format!("{path}: {e}"))?;
            derive_spans(repo, &spans)?
        }
        _ => return Err("need exactly one of --manifest and --spans".into()),
    };
    let plan = deploy_plan(&parts, &declared, &authorized, &probe);
    if let Some(out) = flags.get("blockers-out") {
        let text = serde_json::to_vec_pretty(&plan.blocker_items()).map_err(|e| e.to_string())?;
        std::fs::write(out, text).map_err(|e| format!("{out}: {e}"))?;
    }
    Ok(plan.report())
}
