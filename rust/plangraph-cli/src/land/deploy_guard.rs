//! `land-train deploy-guard [--refresh yes] [--deploy-ledgers DIR] [--hosts FILE]`: the deploy part of the
//! GO on its own, for the landing step that merges or fast-forwards a train.
//!
//! With `--refresh yes` it first asks every host of the hosts registry for a fresh plan and ledger
//! (`host-sync --install --check --fleet`, which changes nothing on a host), so the verdict never rests on
//! a ledger an earlier run left behind. It then refuses while any expected host has no fresh ledger or any
//! entry is over 24 hours not live without a dated exception ([`super::go_verify::deploy_refusals`]).
use super::go_verify::{
    deploy_ledger_dir, deploy_refusals, deploy_registry_text, now_secs, DEPLOY_EXCEPTIONS,
};
use crate::deploy_manifest::fleet::{hosts_from_registry, run_fleet, Remote};
use std::collections::BTreeMap;

pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    run_with(flags, &crate::deploy_manifest::fleet::Processes)
}

pub(super) fn run_with(
    flags: &BTreeMap<String, String>,
    remote: &dyn Remote,
) -> Result<Vec<String>, String> {
    let ledgers = deploy_ledger_dir(flags);
    let registry = deploy_registry_text(flags);
    let mut lines = Vec::new();
    if flags.get("refresh").is_some_and(|v| v == "yes") {
        let text = registry
            .as_deref()
            .ok_or("deploy-guard: the hosts registry is not readable")?;
        let hosts = hosts_from_registry(text)?;
        let (table, _) = run_fleet(&hosts, remote, true, &ledgers);
        lines.extend(table);
    }
    let refusals = deploy_refusals(&ledgers, DEPLOY_EXCEPTIONS, registry.as_deref(), now_secs())?;
    if refusals.is_empty() {
        lines.push("DEPLOY-LIVE every expected host reports and no entry is over its limit".into());
        return Ok(lines);
    }
    let reasons: Vec<_> = refusals
        .iter()
        .map(|r| format!("{}: {}", r.code, r.detail))
        .collect();
    Err(format!("deploy refused:\n{}", reasons.join("\n")))
}
