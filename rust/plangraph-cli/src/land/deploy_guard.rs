//! `land-train deploy-guard [--refresh yes] [--deploy-ledgers DIR] [--hosts FILE]`: the deploy part of the
//! GO on its own, for the landing step that merges or fast-forwards a train.
//!
//! With `--refresh yes` it first asks every host of the hosts registry for a fresh plan and ledger
//! (`host-sync --install --check --fleet`, which changes nothing on a host) and refuses a host that hands
//! none back (`deploy_refresh_failed`), so the verdict never rests on a ledger an earlier run left behind. It then refuses while any expected host has no fresh ledger or any
//! entry is over 24 hours not live without a dated exception ([`super::go_verify::deploy_refusals`]).
use super::go_verify::{
    deploy_ledger_dir, deploy_pins, deploy_refusals, deploy_registry_text, now_secs,
    DEPLOY_EXCEPTIONS,
};
use crate::deploy_manifest::fleet::{hosts_from_registry, run_fleet_results, Remote};
use crate::deploy_manifest::guard::expected_hosts;
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
    let mut reasons = Vec::new();
    if flags.get("refresh").is_some_and(|v| v == "yes") {
        let text = registry
            .as_deref()
            .ok_or("deploy-guard: the hosts registry is not readable")?;
        let hosts = hosts_from_registry(text)?;
        let (table, _, results) = run_fleet_results(&hosts, remote, true, &ledgers);
        lines.extend(table);
        // A cached ledger is not an answer: every expected host must have handed back a ledger on this
        // refresh. A collection failure is named as such, apart from entries inside the 24-hour grace.
        for host in expected_hosts(text)? {
            match results.iter().find(|r| r.id == host.id) {
                Some(r) if r.failure.is_none() && r.ledger.is_some() => {}
                Some(r) => reasons.push(format!(
                    "deploy_refresh_failed: {}: {}",
                    host.id,
                    r.failure.as_deref().unwrap_or("no ledger came back")
                )),
                None => reasons.push(format!("deploy_refresh_failed: {}: not asked", host.id)),
            }
        }
    }
    let pins = deploy_pins(flags);
    let refusals = deploy_refusals(
        &ledgers,
        DEPLOY_EXCEPTIONS,
        registry.as_deref(),
        pins.as_ref(),
        now_secs(),
    )?;
    reasons.extend(refusals.iter().map(|r| format!("{}: {}", r.code, r.detail)));
    if reasons.is_empty() {
        lines.push("DEPLOY-LIVE every expected host reports and no entry is over its limit".into());
        return Ok(lines);
    }
    Err(format!("deploy refused:\n{}", reasons.join("\n")))
}
