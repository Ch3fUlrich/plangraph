//! Renew the orchestrator's held claims from the live writer roster.
//!
//! `plangraph claim-heartbeat` intersects the claims of this principal with the items live
//! native runs, Claude writer sessions and open pull requests hold, renews exactly those and
//! reports every renewal, refusal and unrenewed claim with its item key. A merged item that
//! waits for its close pass needs no lease and is never renewed.
use crate::keeper_events::Client;
use pg_worker_core::lane_guard::{heartbeat_plan, Claim};
use pg_worker_core::lane_roster::{local_lanes, pull_request_lanes, Lane};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

const BATCH: usize = 200;
const SNAPSHOT: &str =
    "v1/snapshot?fmt=json&open=1&blocks=items&fields=state,epoch,claimed_by,merged";

pub trait Keeper {
    fn principal(&mut self) -> Result<String, String>;
    fn claims(&mut self) -> Result<Vec<Claim>, String>;
    /// One `heartbeat_batch` call of at most `BATCH` `(key, epoch)` pairs; one row per key.
    fn heartbeat(&mut self, claims: &[(String, i64)]) -> Result<Vec<Value>, String>;
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Report {
    pub lines: Vec<String>,
    pub renewed: usize,
    pub refused: usize,
}

pub fn claims_from_snapshot(snapshot: &Value) -> Vec<Claim> {
    snapshot["items"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|item| {
            Some(Claim {
                key: item["key"].as_str()?.to_owned(),
                epoch: item["epoch"].as_i64().unwrap_or(0),
                state: item["state"].as_str()?.to_owned(),
                claimed_by: item["claimed_by"].as_str().unwrap_or("").to_owned(),
                merged: item["merged"].as_bool().unwrap_or(false),
            })
        })
        .collect()
}

/// One pass: plan from the claims and lanes, renew, and report each key.
pub fn pass(keeper: &mut impl Keeper, lanes: &[Lane]) -> Result<Report, String> {
    let principal = keeper.principal()?;
    let claims = keeper.claims()?;
    let plan = heartbeat_plan(&claims, &principal, lanes);
    let mut report = Report::default();
    for (key, reason) in &plan.skipped {
        // Another holder's or unclaimed items are not this principal's to report.
        if matches!(*reason, "no_live_lane" | "merged") {
            report.lines.push(format!("skipped {key} {reason}"));
        }
    }
    for chunk in plan.renew.chunks(BATCH) {
        for row in keeper.heartbeat(chunk)? {
            let key = row["key"].as_str().unwrap_or("?");
            if let Some(code) = row["error"].as_str() {
                report.refused += 1;
                let msg = row["msg"].as_str().unwrap_or("");
                report.lines.push(format!("refused {key} {code}: {msg}"));
            } else {
                report.renewed += 1;
                let (epoch, lease) = (&row["epoch"], row["lease_until"].as_str().unwrap_or("?"));
                report
                    .lines
                    .push(format!("renewed {key} epoch {epoch} lease {lease}"));
            }
        }
    }
    report.lines.push(format!(
        "claim-heartbeat: {} renewed, {} refused, {} not renewed",
        report.renewed,
        report.refused,
        plan.skipped.len()
    ));
    Ok(report)
}

struct Http(Client);

impl Keeper for Http {
    fn principal(&mut self) -> Result<String, String> {
        let who: Value = self.0.get("v1/whoami?fmt=json")?;
        who["name"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| "whoami names no principal".into())
    }
    fn claims(&mut self) -> Result<Vec<Claim>, String> {
        Ok(claims_from_snapshot(&self.0.get::<Value>(SNAPSHOT)?))
    }
    fn heartbeat(&mut self, claims: &[(String, i64)]) -> Result<Vec<Value>, String> {
        let rows: Vec<Value> = claims
            .iter()
            .map(|(key, epoch)| json!({"key":key,"epoch":epoch}))
            .collect();
        let mut id = [0u8; 16];
        getrandom::fill(&mut id).map_err(|_| "no randomness for the idempotency key")?;
        let key: String = id.iter().map(|b| format!("{b:02x}")).collect();
        let reply = self
            .0
            .post("v1/heartbeat_batch?fmt=json", &key, &json!({"claims":rows}))?;
        reply["results"]
            .as_array()
            .cloned()
            .ok_or_else(|| "heartbeat_batch returned no results".into())
    }
}

fn open_pull_requests() -> Result<Vec<Lane>, String> {
    let out = std::process::Command::new("gh")
        .args(["pr", "list", "--state", "open", "--limit", "200"])
        .args(["--json", "number,headRefName,title,body"])
        .output()
        .map_err(|e| format!("gh pr list: {e}"))?;
    if !out.status.success() {
        return Err("gh pr list failed".into());
    }
    Ok(pull_request_lanes(&String::from_utf8_lossy(&out.stdout)))
}

pub fn run(env: &BTreeMap<String, String>, home: &Path, args: &[String]) -> i32 {
    let dry = args == ["--dry-run"];
    if !args.is_empty() && !dry {
        eprintln!("usage: plangraph claim-heartbeat [--dry-run]");
        return 2;
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let routing = std::env::var_os("ROUTING_STATE_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| home.join(".local/state/routing"));
    let state = std::env::var_os("PLANGRAPH_SPAWN_STATE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| crate::paths::state(home));
    let mut lanes = local_lanes(&state, &routing.join("launch/sessions"), now);
    let mut degraded = false;
    match open_pull_requests() {
        Ok(prs) => lanes.extend(prs),
        Err(error) => {
            // Without the pull request source fewer claims are renewed, never more.
            eprintln!("claim-heartbeat: {error}; open pull requests not counted");
            degraded = true;
        }
    }
    let client = match Client::new(env) {
        Ok(client) => client,
        Err(error) => {
            eprintln!("claim-heartbeat: {error}");
            return 2;
        }
    };
    let mut keeper: Box<dyn Keeper> = if dry {
        Box::new(DryRun(Http(client)))
    } else {
        Box::new(Http(client))
    };
    match pass(&mut keeper, &lanes) {
        Ok(report) => {
            report.lines.iter().for_each(|line| println!("{line}"));
            i32::from(report.refused > 0 || degraded)
        }
        Err(error) => {
            eprintln!("claim-heartbeat: {error}");
            2
        }
    }
}

/// Plans against the live keeper but renews nothing.
struct DryRun(Http);

impl Keeper for DryRun {
    fn principal(&mut self) -> Result<String, String> {
        self.0.principal()
    }
    fn claims(&mut self) -> Result<Vec<Claim>, String> {
        self.0.claims()
    }
    fn heartbeat(&mut self, claims: &[(String, i64)]) -> Result<Vec<Value>, String> {
        Ok(claims
            .iter()
            .map(|(key, epoch)| json!({"key":key,"epoch":epoch,"lease_until":"dry-run"}))
            .collect())
    }
}

impl Keeper for Box<dyn Keeper> {
    fn principal(&mut self) -> Result<String, String> {
        (**self).principal()
    }
    fn claims(&mut self) -> Result<Vec<Claim>, String> {
        (**self).claims()
    }
    fn heartbeat(&mut self, claims: &[(String, i64)]) -> Result<Vec<Value>, String> {
        (**self).heartbeat(claims)
    }
}

#[cfg(test)]
#[path = "claim_heartbeat_tests.rs"]
mod tests;
