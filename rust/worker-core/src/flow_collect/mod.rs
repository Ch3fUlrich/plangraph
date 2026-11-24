//! Assemble `flow-snapshot.json` for the flow-watchdog checker from raw readings.
//!
//! The readers (keeper, git, usage, CI, units, fleet series) live in the caller. A reading
//! that is `null` or missing is a failed read: the fields it feeds are left out of the
//! snapshot, so the checker reports `invalid snapshot` instead of a made-up healthy zero.
mod derive;
mod stuck;

use serde::Deserialize;
use serde_json::{json, Map, Value};

pub use derive::parse_time;

#[derive(Default, Deserialize)]
pub struct Readings {
    pub now: f64,
    /// Weighted daily Claude pace and budget (`tools/claude_usage.py pace --json`).
    pub usage: Option<Usage>,
    /// Completed CI runs of the window, newest or oldest first.
    pub ci: Option<Vec<CiRun>>,
    /// Rows of the keeper snapshot.
    pub items: Option<Vec<Item>>,
    /// Keeper activity feed: closes and review rounds.
    pub activity: Option<Vec<Event>>,
    /// The keeper item whose open dependencies are the critical path (the stall service's target).
    pub critical_target: Option<String>,
    /// Keeper's agent-ready result: open items an agent may take, landing-train parts included.
    pub ready_keys: Option<Vec<String>>,
    /// Per host code checkout and disk readings, passed through to `hosts`.
    pub hosts: Option<Vec<Host>>,
    /// Installed unit file checksum against the checkout, per host.
    pub units: Option<Vec<Unit>>,
    /// Newest fleet-series sample per host (the one host probe).
    pub series: Option<Vec<Sample>>,
    /// Series hosts that are the coding VMs; runs there count for `coding_vm_runs`.
    #[serde(default)]
    pub coding_hosts: Vec<String>,
    /// Series hosts that run workers; any idle one with ready work sets `worker_idle`.
    #[serde(default)]
    pub worker_hosts: Vec<String>,
}

#[derive(Deserialize)]
pub struct Usage {
    pub weighted: f64,
    pub budget_per_day: f64,
}

#[derive(Deserialize)]
pub struct CiRun {
    pub conclusion: String,
    pub created: String,
}

#[derive(Deserialize)]
pub struct Item {
    pub key: String,
    pub state: String,
    #[serde(default)]
    pub owner: Option<String>,
    #[serde(default)]
    pub claimed_by: Option<String>,
    #[serde(default)]
    pub lease_until: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
    /// Keeper `last_activity.at`; read for claimed and in-review critical-path items.
    #[serde(default)]
    pub last_activity_at: Option<String>,
    #[serde(default)]
    pub depends_on: Value,
    #[serde(default)]
    pub waits_on_operator: bool,
    #[serde(default)]
    pub merged: bool,
}

#[derive(Deserialize)]
pub struct Event {
    pub ts: String,
    pub key: String,
    #[serde(rename = "type")]
    pub kind: String,
}

#[derive(Deserialize)]
pub struct Host {
    pub host: String,
    pub tmp_used_percent: f64,
    pub root_used_percent: f64,
    #[serde(default)]
    pub work_used_percent: Vec<f64>,
    pub checkout_head: String,
    pub origin_main_head: String,
    pub behind_commits: u64,
    pub age_hours: f64,
}

#[derive(Deserialize)]
pub struct Unit {
    pub host: String,
    pub name: String,
    pub deployed_sha: Option<String>,
    pub expected_sha: String,
}

#[derive(Deserialize)]
pub struct Sample {
    pub host: String,
    pub ts: f64,
    pub runs: u64,
    pub ready: u64,
    pub reachable: bool,
    #[serde(default)]
    pub sessions: Vec<String>,
}

/// Inputs the collector does not read yet. They are named in every snapshot so their
/// absence cannot be mistaken for a healthy reading.
pub const ABSENT_INPUTS: [&str; 8] = [
    "leg_state",
    "review_leg_activity",
    "writer_activity",
    "session_activity",
    "go_requests",
    "ordered_items",
    "idle_capacity",
    "host_idle_capacity",
];

/// Readings older than this many seconds are not current evidence.
pub const SERIES_MAX_AGE_S: f64 = 300.0;

/// Decode each reading on its own: a malformed source is a gap for that source only.
/// `now` is the one reading without which nothing can be judged.
pub fn decode(value: Value) -> Result<(Readings, Vec<String>), String> {
    let Value::Object(mut map) = value else {
        return Err("readings must be a JSON object".into());
    };
    let mut gaps = Vec::new();
    fn take<T: serde::de::DeserializeOwned>(
        map: &mut Map<String, Value>,
        name: &str,
        gaps: &mut Vec<String>,
    ) -> Option<T> {
        match serde_json::from_value(map.remove(name)?) {
            Ok(value) => value,
            Err(error) => {
                gaps.push(format!("{name}: malformed reading ({error})"));
                None
            }
        }
    }
    let now: f64 = take(&mut map, "now", &mut gaps).ok_or("readings have no usable now")?;
    let readings = Readings {
        now,
        usage: take(&mut map, "usage", &mut gaps),
        ci: take(&mut map, "ci", &mut gaps),
        items: take(&mut map, "items", &mut gaps),
        activity: take(&mut map, "activity", &mut gaps),
        critical_target: take(&mut map, "critical_target", &mut gaps),
        ready_keys: take(&mut map, "ready_keys", &mut gaps),
        hosts: take(&mut map, "hosts", &mut gaps),
        units: take(&mut map, "units", &mut gaps),
        series: take(&mut map, "series", &mut gaps),
        coding_hosts: take(&mut map, "coding_hosts", &mut gaps).unwrap_or_default(),
        worker_hosts: take(&mut map, "worker_hosts", &mut gaps).unwrap_or_default(),
    };
    Ok((readings, gaps))
}

pub struct Assembled {
    pub snapshot: Value,
    /// One line per field group that could not be filled, with the reason.
    pub gaps: Vec<String>,
}

pub fn assemble(readings: &Readings) -> Assembled {
    assemble_with_gaps(readings, Vec::new())
}

/// `gaps` are the readings [`decode`] could not read; they join the ones found here.
pub fn assemble_with_gaps(readings: &Readings, mut gaps: Vec<String>) -> Assembled {
    let mut out = Map::new();
    let mut put = |group: &str, result: Result<Map<String, Value>, String>| match result {
        Ok(fields) => out.extend(fields),
        Err(reason) => gaps.push(format!("{group}: {reason}")),
    };
    put("usage", derive::usage(readings));
    put("ci", derive::ci(readings));
    put("review", derive::review(readings));
    put("critical_path", derive::critical(readings));
    put("placement", derive::placement(readings));
    put("host_drift", derive::drift(readings));
    put("closes", derive::closes(readings));
    put("hosts", derive::hosts(readings));
    put("stuck", stuck::rows(readings));
    put("ready_owners", stuck::ready_owners(readings));
    put("ready_families", stuck::families(readings));
    out.insert("collected_at".into(), json!(readings.now));
    out.insert("absent_inputs".into(), json!(ABSENT_INPUTS));
    // The checker reads a snapshot with gaps as invalid, also when only optional blocks failed.
    out.insert("collector_gaps".into(), json!(gaps));
    Assembled {
        snapshot: Value::Object(out),
        gaps,
    }
}
