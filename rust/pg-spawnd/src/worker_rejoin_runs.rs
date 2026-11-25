//! Runs marked unknown during an outage: what became of them, and settling the record.
use super::{Inventory, Ssh};
use pg_worker_core::{
    run::record::{RunRecord, Status},
    worker_host::RunFate,
};
use serde_json::{json, Value};
use std::{io, process::Command, time::Duration};

use super::now;

/// Only a Keeper refusal (exit 2) can say the original claim is gone; a transport
/// failure (exit 3) with a similar diagnostic proves nothing.
fn settled_by_refusal(code: Option<i32>, stderr: &str, epoch: u64) -> bool {
    code == Some(2) && (stderr.contains("not claimed") || superseded(stderr, epoch))
}

/// The Keeper refuses a mismatched epoch in either direction ("epoch A != current B"). Only a
/// newer current epoch shows the original claim was replaced; an older one means the
/// original claim is not what the Keeper holds, which settles nothing.
fn superseded(stderr: &str, asked: u64) -> bool {
    let words: Vec<_> = stderr.split_whitespace().collect();
    words.windows(5).any(|w| {
        w[0] == "epoch"
            && w[1].parse::<u64>().ok() == Some(asked)
            && w[2] == "!="
            && w[3] == "current"
            && w[4]
                .trim_end_matches(|c: char| !c.is_ascii_digit())
                .parse::<u64>()
                .is_ok_and(|current| current > asked)
    })
}

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
    /// (`claim_epoch`). The release verb is a write that never falls back to a local
    /// snapshot, so its receipt (`{"key": ITEM}`) is the proof. A refusal that says the
    /// item is not claimed, or that the epoch is stale, means the original claim is
    /// already gone (released, or replaced by a newer claim that is left alone). Any
    /// other outcome keeps the run unsettled. A run with no recorded epoch is not
    /// released: its claim cannot be told from a newer one.
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
        let out = pg_worker_core::host_command::capture(
            Command::new(&cli)
                .env("PG_EPOCH", epoch.to_string())
                .args(["release", item, "--json"]),
            Duration::from_secs(60),
        )?;
        let stderr = String::from_utf8_lossy(&out.stderr);
        if out.status.success() {
            let receipt: Value = serde_json::from_slice(&out.stdout).unwrap_or(Value::Null);
            return if receipt["key"] == item {
                Ok(())
            } else {
                Err(io::Error::other(format!(
                    "the release of {item} returned no receipt for it"
                )))
            };
        }
        if settled_by_refusal(out.status.code(), &stderr, epoch) {
            return Ok(());
        }
        Err(io::Error::other(format!(
            "the claim on {item} was not confirmed released: {}",
            stderr.trim()
        )))
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

#[cfg(test)]
mod tests {
    use super::{settled_by_refusal, superseded};

    #[test]
    fn a_transport_failure_never_settles_a_claim() {
        let text = "keeper unreachable: epoch 11 != current 12";
        assert!(!settled_by_refusal(Some(3), text, 11));
        assert!(!settled_by_refusal(None, text, 11));
        assert!(settled_by_refusal(
            Some(2),
            "epoch_stale: epoch 11 != current 12",
            11
        ));
        assert!(settled_by_refusal(
            Some(2),
            "bad_state: item is open, not claimed",
            11
        ));
        assert!(!settled_by_refusal(
            Some(2),
            "forbidden: only the claimant may release",
            11
        ));
    }

    #[test]
    fn only_a_newer_current_epoch_shows_the_claim_was_superseded() {
        assert!(superseded(
            "refused: epoch_stale: epoch 11 != current 12",
            11
        ));
        assert!(!superseded(
            "refused: epoch_stale: epoch 12 != current 11",
            12
        ));
        assert!(!superseded(
            "refused: epoch_stale: epoch 11 != current 11",
            11
        ));
        assert!(!superseded(
            "refused: epoch_stale: epoch 9 != current 12",
            11
        ));
        assert!(!superseded("keeper unreachable", 11));
    }
}
