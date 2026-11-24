//! Runs marked unknown during an outage: what became of them, and settling the record.
use super::{Inventory, Ssh};
use pg_worker_core::{
    run::record::{RunRecord, Status},
    worker_host::RunFate,
};
use serde_json::{json, Value};
use std::{io, process::Command, time::Duration};

use super::now;

impl Ssh<'_> {
    /// The host's view of its runs: sessions verified, terminal records read from disk.
    fn inventory(&mut self) -> io::Result<&Inventory> {
        if self.inventory.is_none() {
            let out = self.ok(&["pg-spawnd", "worker-active-runs", &self.host.id], 60)?;
            let receipt: Value = serde_json::from_slice(&out).map_err(io::Error::other)?;
            let ids = |key: &str| {
                receipt[key]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|id| id.as_str().map(str::to_owned))
                    .collect()
            };
            self.inventory = Some(Inventory {
                live: ids("live"),
                queued: ids("queued"),
                unverified: ids("unverified"),
                dead: ids("dead"),
                terminal: receipt["terminal"]
                    .as_object()
                    .into_iter()
                    .flatten()
                    .map(|(id, row)| (id.clone(), row.clone()))
                    .collect(),
            });
        }
        Ok(self.inventory.as_ref().expect("inventory read above"))
    }

    /// A run in none of the host's lists is uncertain and blocks the gate.
    pub(super) fn classify(&mut self, run: &str) -> io::Result<RunFate> {
        let inventory = self.inventory()?;
        if inventory.live.contains(run) {
            Ok(RunFate::Live)
        } else if inventory.queued.contains(run) {
            Ok(RunFate::Queued)
        } else if inventory.unverified.contains(run) {
            Err(io::Error::other(format!(
                "{run} is recorded as running with no process identity; its outcome is uncertain"
            )))
        } else if inventory.terminal.contains_key(run) {
            Ok(RunFate::Finished)
        } else if inventory.dead.contains(run) {
            Ok(RunFate::Dead)
        } else {
            Err(io::Error::other(format!(
                "{run} is in no record of the host; its outcome is uncertain"
            )))
        }
    }

    /// Release the Keeper claim a dead run held, with the epoch the run recorded
    /// (`claim_epoch`), then read the item back. A newer claim by another run has a
    /// newer epoch and is refused by the Keeper; a run with no recorded epoch is
    /// not released here because its claim cannot be told from a newer one.
    fn release_claim(&self, item: &str, epoch: Option<u64>) -> io::Result<()> {
        let epoch = epoch.ok_or_else(|| {
            io::Error::other(format!(
                "dead run holds {item} with no recorded claim_epoch; release it by hand"
            ))
        })?;
        let cli = std::env::var("PG_KEEPER_CLI").map_err(|_| {
            io::Error::other(format!(
                "PG_KEEPER_CLI is not set; the claim on {item} is not released"
            ))
        })?;
        let call = |verb: &[&str]| {
            pg_worker_core::host_command::capture(
                Command::new(&cli)
                    .env("PG_EPOCH", epoch.to_string())
                    .args(verb),
                Duration::from_secs(60),
            )
        };
        let released = call(&["release", item])?;
        let shown = call(&["show", item, "--json"])?;
        let state: Value = serde_json::from_slice(&shown.stdout).unwrap_or(Value::Null);
        // Another run's newer claim is left alone and is not this run's to release.
        if state["state"] == "claimed" && state["epoch"].as_u64().is_some_and(|now| now > epoch) {
            return Ok(());
        }
        if state["state"] == "claimed" || !shown.status.success() {
            return Err(io::Error::other(format!(
                "the claim on {item} is still held after release ({})",
                String::from_utf8_lossy(&released.stderr).trim()
            )));
        }
        Ok(())
    }

    pub(super) fn settle(&mut self, run: &str, fate: RunFate) -> io::Result<()> {
        let mut record = match RunRecord::read(&self.state, run) {
            Ok(record) => record,
            // The run never reached this coordinator's records.
            Err(pg_worker_core::run::record::RecordError::Unknown) => return Ok(()),
            Err(error) => return Err(io::Error::other(error.to_string())),
        };
        let was_unknown = record.get("status").and_then(Value::as_str) == Some("unknown");
        if matches!(fate, RunFate::Live | RunFate::Queued) {
            // The record is made consistent; the host's reconciler follows the session
            // (a live run) or the queue (a queued run) from here.
            if was_unknown {
                let (status, event) = match fate {
                    RunFate::Live => ("running", "REJOIN_REATTACHED"),
                    _ => ("queued", "REJOIN_QUEUED"),
                };
                record.set("status", json!(status));
                return record
                    .push_event(json!({"event": event, "at": now()}))
                    .and_then(|()| record.save())
                    .map_err(|error| io::Error::other(error.to_string()));
            }
            return Ok(());
        }
        if was_unknown {
            record.set("status", json!("running"));
        }
        if record.status().is_none_or(Status::is_terminal) {
            return Ok(());
        }
        // A queued run walks queued -> running first; only the statuses that can end a
        // running run are kept, and refused, limited or dry-run ends count as failed.
        if record.status() == Some(Status::Queued) {
            record.set("status", json!("running"));
        }
        let remote = self
            .inventory
            .as_ref()
            .and_then(|inventory| inventory.terminal.get(run))
            .cloned()
            .unwrap_or(Value::Null);
        let to = match fate {
            RunFate::Finished => remote
                .get("status")
                .and_then(|status| Status::parse(status.as_str()?))
                .filter(|status| {
                    !matches!(
                        status,
                        Status::Queued
                            | Status::Running
                            | Status::Refused
                            | Status::Limited
                            | Status::DryRun
                    )
                })
                .unwrap_or(Status::Failed),
            _ => Status::Failed,
        };
        // A dead run's claim is released first: if that fails nothing is recorded as settled,
        // so the next rejoin finds the run unsettled and tries again.
        if fate == RunFate::Dead {
            if let Some(item) = record.get("item_key").and_then(Value::as_str) {
                let epoch = record.get("claim_epoch").and_then(Value::as_u64);
                self.release_claim(item, epoch)?;
            }
        }
        let at = now();
        // The host's own terminal result is kept on the record: when it ended and its exit code.
        record
            .transition(to, at as f64)
            .map_err(|error| io::Error::other(error.to_string()))?;
        if fate == RunFate::Finished {
            for key in ["ended", "exit_code"] {
                if let Some(value) = remote.get(key).filter(|value| !value.is_null()) {
                    record.set(key, value.clone());
                }
            }
        }
        // Claims and lanes follow the record: a status that keeps them would leave the run held.
        if fate == RunFate::Dead && !pg_worker_core::run::record::releases_ownership(to.as_str()) {
            return Err(io::Error::other(format!(
                "{run} ended as {} which keeps its claim and lane",
                to.as_str()
            )));
        }
        record
            .push_event(json!({
                "event": "REJOIN_SETTLED", "fate": fate, "at": at,
                "status": to.as_str(), "remote_exit_code": remote.get("exit_code"),
            }))
            .and_then(|()| record.save())
            .map_err(|error| io::Error::other(error.to_string()))
    }
}
