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
    /// Oneshot service: its `InvocationID=`, which the first run must match the one the install started.
    #[serde(default)]
    pub invocation: Option<String>,
}

/// What the step verifies a first run against: the commit time of the verified landed head (microseconds),
/// the clock in microseconds (read after a target's first run is read), and the (family, host) targets the
/// preflight refused.
#[derive(Clone, Debug)]
pub struct Gate {
    pub fresh_after: i64,
    pub clock: fn() -> i64,
    pub excluded: BTreeSet<(String, String)>,
}

/// `is_active` a read reports for a unit that is not installed yet: the baseline before an install.
pub const ABSENT: &str = "absent";

/// An exit time as RFC 3339 with its fractional seconds, the form the keeper's deploy verifier reads (it
/// rejects a fraction in the `Sun 2026-11-01 12:01:00 UTC` form).
pub fn exit_rfc3339(text: &str) -> Option<String> {
    use chrono::{DateTime, SecondsFormat};
    DateTime::from_timestamp_micros(exit_micros(text)?)
        .map(|d| d.to_rfc3339_opts(SecondsFormat::AutoSi, true))
}

/// Microseconds since the epoch of a systemd exit timestamp, keeping its fractional seconds like the keeper
/// does: RFC 3339, or the `Sun 2026-11-01 12:01:00 UTC` form (also with a fraction).
pub fn exit_micros(text: &str) -> Option<i64> {
    use chrono::{DateTime, NaiveDateTime};
    let text = text.trim();
    DateTime::parse_from_rfc3339(text)
        .map(|d| d.timestamp_micros())
        .or_else(|_| {
            NaiveDateTime::parse_from_str(text.trim_end_matches(" UTC"), "%a %Y-%m-%d %H:%M:%S%.f")
                .map(|d| d.and_utc().timestamp_micros())
        })
        .ok()
}

impl RunRead {
    /// Whether the first run counts as good: a oneshot reports `success` and an exit time after the landed
    /// head, strictly after the exit time it showed before this install (`before`: the run is a new one, not
    /// the previous deploy's) and not after `now` (microseconds, as the keeper compares them), and it must be the invocation the install
    /// started (`started`), not a timer's run in between; a binary is `installed`, anything
    /// else is `active`.
    pub fn good(
        &self,
        family: &str,
        gate: &Gate,
        before: Option<i64>,
        now: i64,
        started: Option<&str>,
    ) -> bool {
        if self.oneshot {
            let exited = self
                .exec_main_exit_timestamp
                .as_deref()
                .and_then(exit_micros);
            return self.result.as_deref() == Some("success")
                && started.is_some()
                && self.invocation.as_deref() == started
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
    /// Installs the family and returns the invocation it started (a oneshot's `InvocationID`), if any.
    fn install(&self, host: &str, family: &str) -> Result<Option<String>, String>;
    fn read_run(&self, host: &str, family: &str) -> Result<RunRead, String>;
    fn disable(&self, host: &str, unit: &str) -> Result<(), String>;
}

/// Where the deploy rows go (the keeper).
pub trait RowSink {
    fn write(&self, row: &DeployRow) -> Result<(), String>;
}
