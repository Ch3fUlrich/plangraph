//! `land-train deploy-plan --repo DIR (--manifest FILE | --base SHA --head SHA) [--role-hosts H=R+R,...]
//! [--hosts FILE] [--authorized-hosts A,B] [--probe-cmd CMD | --probe hosts] [--declared FILE]
//! [--blockers-out FILE] [--plan-out FILE]`: the deploy table of a built train (`--manifest`) or of a landed
//! range (`--base --head`, e.g. a merged train against the main it landed on).
//!
//! The hosts holding each manifest role come from `--role-hosts`, else from `PG_HOST_ROLES` in each host's
//! env of the registry `--hosts`. The authorized hosts default to those hosts. `--probe-cmd` runs
//! `CMD HOST UNIT` as the install dry run; `--probe hosts` runs `deploy-host --op check` on each host of the
//! registry at `--head`; without either the targets are not probed. `--declared` is a JSON object of item key
//! -> hosts from the items' `Deploy-Hosts:` lines. `--plan-out` saves the plan for `deploy-apply`.
use super::deploy::*;
use super::deploy_apply::HostCmd;
use pg_worker_core::hosts::HostRegistry;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

fn registry_roles(registry: &HostRegistry) -> Result<RoleHosts, String> {
    registry
        .hosts
        .iter()
        .filter_map(|h| h.env.get("PG_HOST_ROLES").map(|r| (h.id.clone(), r)))
        .map(|(h, r)| Ok((h, crate::deploy_manifest::host_roles(Some(r), None)?)))
        .collect()
}

pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let read = |p: &str| std::fs::read(p).map_err(|e| format!("{p}: {e}"));
    let repo = Path::new(need("repo")?);
    let declared: BTreeMap<String, Vec<String>> = match flags.get("declared") {
        Some(p) => serde_json::from_slice(&read(p)?).map_err(|e| format!("{p}: {e}"))?,
        None => BTreeMap::new(),
    };
    let registry = match flags.get("hosts") {
        Some(p) => Some(HostRegistry::load(Path::new(p)).map_err(|e| format!("{p}: {e}"))?),
        None => None,
    };
    let role_hosts = match (flags.get("role-hosts"), &registry) {
        (Some(spec), _) => parse_role_hosts(spec)?,
        (None, Some(r)) => registry_roles(r)?,
        (None, None) => RoleHosts::new(),
    };
    let authorized: BTreeSet<String> = match flags.get("authorized-hosts") {
        Some(list) => list
            .split(',')
            .filter(|h| !h.is_empty())
            .map(str::to_owned)
            .collect(),
        None => role_hosts.keys().cloned().collect(),
    };
    let (parts, head) = match flags.get("manifest") {
        Some(p) => {
            let manifest: super::train::Manifest =
                serde_json::from_slice(&read(p)?).map_err(|e| e.to_string())?;
            (derive(repo, &manifest)?, manifest.train_sha)
        }
        None => (
            derive_landed(repo, need("base")?, need("head")?)?,
            need("head")?.clone(),
        ),
    };
    let probe: Box<dyn Probe> = match (
        flags.get("probe-cmd"),
        flags.get("probe").map(String::as_str),
        registry,
    ) {
        (Some(cmd), _, _) => Box::new(CommandProbe {
            argv: cmd.split_whitespace().map(str::to_owned).collect(),
        }),
        (None, Some("hosts"), Some(registry)) => Box::new(HostCmd {
            registry,
            repo_id: flags
                .get("repo-id")
                .cloned()
                .unwrap_or_else(|| "plangraph".into()),
            rev: head,
        }),
        (None, Some(p), _) => {
            return Err(format!("--probe {p} needs --probe hosts and --hosts FILE"))
        }
        (None, None, _) => Box::new(NoProbe),
    };
    let plan = plan_with_roles(&parts, &declared, &authorized, &role_hosts, probe.as_ref());
    if let Some(out) = flags.get("blockers-out") {
        let text = serde_json::to_vec_pretty(&plan.blocker_items()).map_err(|e| e.to_string())?;
        std::fs::write(out, text).map_err(|e| format!("{out}: {e}"))?;
    }
    if let Some(out) = flags.get("plan-out") {
        let text = serde_json::to_vec_pretty(&plan).map_err(|e| e.to_string())?;
        std::fs::write(out, text).map_err(|e| format!("{out}: {e}"))?;
    }
    let mut lines = vec![plan.go_req()];
    if flags.get("probe-cmd").is_none() && flags.get("probe").is_none() {
        lines.push("probe: none (the targets were not dry-run on their hosts)".into());
    }
    for t in &plan.targets {
        let items: Vec<&str> = t.items.iter().map(String::as_str).collect();
        let items = if items.is_empty() {
            "-".to_owned()
        } else {
            items.join(",")
        };
        lines.push(format!("target {}@{} items {items}", t.family, t.host));
    }
    lines.extend(
        plan.skipped
            .iter()
            .map(|(f, why)| format!("skipped {f}: {why}")),
    );
    lines.extend(
        plan.blockers
            .iter()
            .map(|b| format!("blocker {}: {}", b.key, b.reasons.join("; "))),
    );
    Ok(lines)
}
