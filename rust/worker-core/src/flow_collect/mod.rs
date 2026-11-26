//! Assemble the existing watchdog wire format from independent read-only observations.
mod derive;
mod stuck;

use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};

type Fields = Map<String, Value>;
type Evidence<T> = Result<T, String>;
pub const DAY_S: u64 = 86_400;
pub const CI_WINDOW_S: u64 = 7 * DAY_S;
pub const SAMPLE_MAX_AGE_S: u64 = 300;

#[derive(Deserialize)]
struct Usage {
    weighted: f64,
    budget_per_day: f64,
}

#[derive(Deserialize)]
struct CiRun {
    conclusion: String,
    #[serde(alias = "created_at")]
    created: String,
}

#[derive(Deserialize)]
struct Keeper {
    items: Vec<Item>,
    activity: Vec<Event>,
}

#[derive(Deserialize)]
struct Item {
    key: String,
    state: String,
    epoch: u64,
    depends_on: Vec<String>,
    owner: Option<String>,
    claimed_by: Option<String>,
    lease_until: Option<String>,
    updated_at: Option<String>,
}

#[derive(Deserialize)]
struct Event {
    ts: String,
    #[serde(rename = "type")]
    kind: String,
}

#[derive(Deserialize)]
struct Detail {
    key: String,
    epoch: u64,
    state: String,
    round: Option<u64>,
    last_activity: Option<Activity>,
}

#[derive(Deserialize)]
struct Activity {
    at: String,
}

#[derive(Deserialize)]
struct Ready {
    items: Vec<ReadyRow>,
}

#[derive(Deserialize)]
struct ReadyRow {
    key: String,
}

#[derive(Deserialize, Serialize)]
struct Host {
    host: String,
    checkout_head: String,
    origin_main_head: String,
    behind_commits: u64,
    age_hours: f64,
    #[serde(flatten)]
    measurements: Option<Measurements>,
    #[serde(skip_serializing)]
    disk_error: Option<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(untagged)]
enum Measurements {
    Keyed {
        disks: Vec<Disk>,
    },
    Legacy {
        tmp_used_percent: f64,
        root_used_percent: f64,
        #[serde(skip_serializing_if = "Option::is_none")]
        work_used_percent: Option<Vec<f64>>,
    },
}

#[derive(Deserialize, Serialize)]
struct Disk {
    device: String,
    used_percent: f64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    consumers: Vec<Consumer>,
}

#[derive(Deserialize, Serialize)]
struct Consumer {
    path: String,
    bytes: u64,
}

#[derive(Deserialize)]
struct Unit {
    expected_sha: String,
    deployed_sha: Option<String>,
}

#[derive(Deserialize)]
struct LegObservation {
    leg: String,
    account: Option<String>,
    combo: Option<String>,
    observed_at: f64,
    status: u16,
    reset_at: Option<f64>,
    source: String,
}

#[derive(Deserialize)]
struct IdleEvidence {
    observed_at: u64,
    snapshot: Value,
}

/// Diagnostics are grouped by publication impact. The CLI publishes only complete snapshots.
#[derive(Default)]
pub struct Collection {
    pub snapshot: Value,
    pub required_errors: BTreeMap<String, String>,
}

struct Inputs<'a>(&'a Value);

impl Inputs<'_> {
    fn read<T: DeserializeOwned>(&self, name: &str) -> Evidence<T> {
        match self.0.get(name).filter(|value| !value.is_null()) {
            Some(value) => serde_json::from_value(value.clone())
                .map_err(|error| format!("{name} malformed: {error}")),
            None => Err(self.0["source_errors"][name].as_str().map_or_else(
                || format!("{name} unavailable"),
                |error| format!("{name}: {error}"),
            )),
        }
    }
}

fn reference<T>(value: &Evidence<T>) -> Evidence<&T> {
    value.as_ref().map_err(Clone::clone)
}

struct Output {
    fields: Fields,
    required_errors: BTreeMap<String, String>,
    unavailable: BTreeMap<String, String>,
}

impl Output {
    fn required(&mut self, name: &str, result: Evidence<Value>) {
        match result {
            Ok(fields) => self.extend(fields),
            Err(reason) => {
                self.required_errors.insert(name.into(), reason);
            }
        }
    }

    fn optional(&mut self, name: &str, result: Evidence<Value>) {
        match result {
            Ok(fields) => self.extend(fields),
            Err(reason) => {
                self.unavailable.insert(name.into(), reason);
            }
        }
    }

    fn extend(&mut self, fields: Value) {
        if let Value::Object(fields) = fields {
            self.fields.extend(fields);
        }
    }
}

/// Derive from one collection pass. A missing optional reader carries a diagnostic and no
/// watchdog field. Required derivations fail independently so callers can report every gap.
pub fn assemble(raw: &Value) -> Evidence<Collection> {
    let source = Inputs(raw);
    let now = source.read::<u64>("now")?;
    let keeper = source.read::<Keeper>("keeper");
    let details = source.read::<BTreeMap<String, Detail>>("item_details");
    let hosts = source.read::<Vec<Host>>("hosts");
    let series = source.read::<Vec<crate::fleet_series::Sample>>("series");
    let mut out = Output {
        fields: Map::new(),
        required_errors: BTreeMap::new(),
        unavailable: BTreeMap::new(),
    };
    out.fields.insert("collected_at".into(), json!(now));
    out.required("usage", source.read("usage").and_then(derive::usage));
    out.required(
        "ci",
        source.read("ci").and_then(|rows| derive::ci(now, rows)),
    );
    out.required(
        "review",
        reference(&keeper).and_then(|k| derive::review(now, k, &details)),
    );
    out.required(
        "critical_path",
        reference(&keeper).and_then(|k| {
            derive::critical(now, k, &source.read::<String>("critical_target")?, &details)
        }),
    );
    out.required(
        "placement",
        (|| {
            derive::placement(
                now,
                reference(&series)?,
                source.read("coding_hosts")?,
                source.read("worker_hosts")?,
            )
        })(),
    );
    out.required(
        "host_drift",
        reference(&hosts).and_then(|rows| derive::drift(rows, source.read("units")?)),
    );
    out.required(
        "closes",
        reference(&keeper).and_then(|k| derive::closes(now, k)),
    );
    out.optional("hosts", reference(&hosts).and_then(derive::hosts));
    out.optional(
        "keeper_lane_facts",
        reference(&keeper).and_then(|k| stuck::keeper_lanes(now, k)),
    );
    out.optional(
        "ready",
        reference(&keeper).and_then(|k| stuck::ready(k, &source.read("ready")?)),
    );
    out.optional(
        "host_idle_capacity",
        (|| stuck::idle_capacity(now, source.read("host_idle_capacity")?))(),
    );
    out.optional(
        "leg_state",
        (|| {
            let rows = source.read::<Vec<LegObservation>>("leg_state")?;
            if rows.iter().any(|row| {
                row.leg.is_empty()
                    || !row.observed_at.is_finite()
                    || !(0.0..=now as f64).contains(&row.observed_at)
            }) {
                return Err("leg-state observation has an invalid identity or time".into());
            }
            Ok(json!({"leg_state": rows.iter().map(|row| json!({
            "leg": row.leg, "account": row.account, "combo": row.combo,
            "observed_at": row.observed_at, "status": row.status, "reset_at": row.reset_at,
            "source": row.source,
        })).collect::<Vec<_>>() }))
        })(),
    );
    for (name, reason) in [
        (
            "ordered_items",
            "verified typed operator-order evidence unavailable",
        ),
        (
            "go_requests",
            "typed unanswered routing-request evidence unavailable",
        ),
        (
            "idle_capacity",
            "typed current capacity episode start unavailable",
        ),
        (
            "ready_owners",
            "typed owner-session absence episode start unavailable",
        ),
        (
            "merged_reviews",
            "typed protected-merge timestamp for current review unavailable",
        ),
        (
            "review_leg_activity",
            "complete served-review route window unavailable",
        ),
        (
            "writer_activity",
            "typed writer session and review observations unavailable",
        ),
        (
            "session_activity",
            "typed session transcript and run observations unavailable",
        ),
    ] {
        out.unavailable.insert(name.into(), reason.into());
    }
    out.fields
        .insert("unavailable_inputs".into(), json!(out.unavailable));
    Ok(Collection {
        snapshot: Value::Object(out.fields),
        required_errors: out.required_errors,
    })
}

fn detail<'a>(
    details: &'a Evidence<BTreeMap<String, Detail>>,
    item: &Item,
) -> Evidence<&'a Detail> {
    let detail = reference(details)?
        .get(&item.key)
        .ok_or_else(|| format!("no Keeper item details for {}", item.key))?;
    if (detail.key.as_str(), detail.epoch, detail.state.as_str())
        != (item.key.as_str(), item.epoch, item.state.as_str())
    {
        return Err(format!(
            "Keeper item details do not match current state and epoch for {}",
            item.key
        ));
    }
    Ok(detail)
}

fn index(keeper: &Keeper) -> Evidence<BTreeMap<&str, &Item>> {
    let mut items = BTreeMap::new();
    for item in &keeper.items {
        if item.key.is_empty() || items.insert(item.key.as_str(), item).is_some() {
            return Err("Keeper snapshot has an empty or duplicate item key".into());
        }
    }
    Ok(items)
}

fn unique_hosts(hosts: Vec<String>) -> Evidence<BTreeSet<String>> {
    if hosts.is_empty() || hosts.iter().any(|h| !crate::fleet_series::valid_host(h)) {
        return Err("coding and worker host lists must name configured hosts".into());
    }
    Ok(hosts.into_iter().collect())
}
