//! What the deploy step reads and writes: one first run of a target, the gate it is checked against, the
//! keeper deploy row, and the host and keeper sides as traits.
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

/// What one read of the first run of a target reports, in the fields the keeper `deploy` evidence checks.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunRead {
    /// `systemctl is-active` of the unit, or `installed` for a host binary.
    pub is_active: String,
    /// Oneshot service: its `Result=` and `ExecMainExitTimestamp=`.
    #[serde(default)]
    pub result: Option<String>,
    #[serde(default)]
    pub exec_main_exit_timestamp: Option<String>,
    #[serde(default)]
    pub oneshot: bool,
}

/// What the step verifies a first run against: the commit time of the verified landed head, the clock
/// (read after a target's first run is read), and the (family, host) targets the preflight refused.
#[derive(Clone, Debug)]
pub struct Gate {
    pub fresh_after: i64,
    pub clock: fn() -> i64,
    pub excluded: BTreeSet<(String, String)>,
}

/// Epoch seconds of a systemd exit timestamp: RFC 3339, or the `Sun 2026-11-01 12:01:00 UTC` form.
pub fn exit_epoch(text: &str) -> Option<i64> {
    use chrono::{DateTime, NaiveDateTime};
    let text = text.trim();
    DateTime::parse_from_rfc3339(text)
        .map(|d| d.timestamp())
        .or_else(|_| {
            NaiveDateTime::parse_from_str(text.trim_end_matches(" UTC"), "%a %Y-%m-%d %H:%M:%S")
                .map(|d| d.and_utc().timestamp())
        })
        .ok()
}

impl RunRead {
    /// Whether the first run counts as good: a oneshot reports `success` and an exit time after the landed
    /// head, strictly after the exit time it showed before this install (`before`: the run is a new one, not
    /// the previous deploy's) and not after `now`, as the keeper requires; a binary is `installed`, anything
    /// else is `active`.
    pub fn good(&self, family: &str, gate: &Gate, before: Option<i64>, now: i64) -> bool {
        if self.oneshot {
            let exited = self
                .exec_main_exit_timestamp
                .as_deref()
                .and_then(exit_epoch);
            return self.result.as_deref() == Some("success")
                && exited.is_some_and(|e| {
                    e > gate.fresh_after && before.is_none_or(|b| e > b) && e <= now
                });
        }
        let want = if family.starts_with("bin:") {
            "installed"
        } else {
            "active"
        };
        self.is_active == want
    }
}

/// The keeper `deploy` evidence of one (family, host) for one item.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeployRow {
    pub key: String,
    pub host: String,
    pub unit: String,
    pub is_active: String,
    #[serde(rename = "type", skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exec_main_exit_timestamp: Option<String>,
}

/// The host side of the step: install a family, read one run of it, disable a unit.
pub trait Installer {
    fn install(&self, host: &str, family: &str) -> Result<(), String>;
    fn read_run(&self, host: &str, family: &str) -> Result<RunRead, String>;
    fn disable(&self, host: &str, unit: &str) -> Result<(), String>;
}

/// Where the deploy rows go (the keeper).
pub trait RowSink {
    fn write(&self, row: &DeployRow) -> Result<(), String>;
}
