//! What the deploy step reads and writes: one first run of a target, the gate it is checked against, the
//! keeper deploy row, and the host and keeper sides as traits.
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

/// One (family, host) target of the plan the runner is given, with the items that get its deploy rows.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunTarget {
    pub family: String,
    pub host: String,
    pub items: BTreeSet<String>,
}

/// An invocation id that is present and not blank.
fn usable(id: Option<&str>) -> Option<&str> {
    id.map(str::trim).filter(|s| !s.is_empty())
}

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

/// What the step verifies a first run against: the commit time of the verified landed head and the clock
/// (read after a target's first run is read), both in microseconds.
#[derive(Clone, Debug)]
pub struct Gate {
    pub fresh_after: i64,
    pub clock: fn() -> i64,
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
    /// Whether the first run counts as good. A oneshot reports `success` and an exit time after the landed
    /// head, strictly after the one it showed before this install and not after `now` (microseconds, as the
    /// keeper compares them). A service that is not a oneshot must be `active` with `Result=success` (it
    /// started), and both kinds must show the invocation the install started, which is not the one they showed
    /// before it (so a timer's run, or a unit nothing restarted, is no proof). A binary is `installed`,
    /// anything else is `active`.
    pub fn good(
        &self,
        family: &str,
        gate: &Gate,
        base: &Baseline,
        now: i64,
        started: Option<&str>,
    ) -> bool {
        // A restart is proved by a new invocation id. A blank id is no id: an already-running unit whose id
        // we could not read before the install leaves nothing to compare with.
        let (seen, was, started) = (
            usable(self.invocation.as_deref()),
            usable(base.invocation.as_deref()),
            usable(started),
        );
        let fresh =
            started.is_some() && seen == started && seen != was && !(base.active && was.is_none());
        if self.oneshot {
            let exited = self
                .exec_main_exit_timestamp
                .as_deref()
                .and_then(exit_micros);
            return self.result.as_deref() == Some("success")
                && fresh
                && exited.is_some_and(|e| {
                    e > gate.fresh_after && base.exit.is_none_or(|b| e > b) && e <= now
                });
        }
        if family.ends_with(".service") {
            return self.is_active == "active"
                && self.result.as_deref() == Some("success")
                && fresh;
        }
        let want = if family.starts_with("bin:") {
            "installed"
        } else {
            "active"
        };
        self.is_active == want
    }
}

/// What a unit showed before its install: the exit time (microseconds) and the invocation of its last run.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Baseline {
    pub exit: Option<i64>,
    pub invocation: Option<String>,
    /// The unit was already running before the install.
    pub active: bool,
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

/// Progress of the step, saved after each target: `family@host` of every finished target.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PostState {
    #[serde(default)]
    pub done: BTreeSet<String>,
    /// The first failed target: the step stopped there and does not run again until it is cleared.
    #[serde(default)]
    pub failed: Option<String>,
    /// Units a failed attempt could not switch off (`(host, unit)`): the next run retries them and refuses
    /// to go on until they are off.
    #[serde(default)]
    pub cleanup: BTreeSet<(String, String)>,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct PostReport {
    pub deployed: Vec<String>,
    pub skipped_done: Vec<String>,
    /// The failed target and why, when the step stopped.
    pub failed: Option<(String, String)>,
    /// Targets behind the failure, not touched.
    pub not_attempted: Vec<String>,
    pub notes: Vec<String>,
}

impl PostReport {
    pub fn lines(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .deployed
            .iter()
            .map(|d| format!("deployed {d}"))
            .collect();
        out.extend(
            self.skipped_done
                .iter()
                .map(|d| format!("already done {d}")),
        );
        if let Some((t, why)) = &self.failed {
            out.push(format!("failed {t}: {why}"));
        }
        out.extend(
            self.not_attempted
                .iter()
                .map(|d| format!("not attempted {d}")),
        );
        out.extend(self.notes.iter().cloned());
        out
    }
}
