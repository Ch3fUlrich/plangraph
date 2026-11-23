use crate::keeper_events::Client;
use chrono::{DateTime, Utc};
use rusqlite::{Connection, OpenFlags};
use serde::Deserialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    path::Path,
};

const COST_WEIGHT: f64 = 10.0;

#[derive(Debug, Deserialize)]
pub(super) struct Attempt {
    pub attempt_id: String,
    pub card_key: Option<String>,
    pub role: String,
    pub task_class: Option<String>,
    pub leg: Option<String>,
    pub requested_model: Option<String>,
    pub harness: Option<String>,
    pub effort: Option<String>,
    pub started_at: DateTime<Utc>,
    pub exit_class: String,
    pub cost: Option<f64>,
    pub route: Option<Route>,
    pub verified: Option<bool>,
}

#[derive(Debug, Deserialize)]
pub(super) struct Route {
    pub eligible_n: usize,
    pub pin: bool,
    pub top5: Vec<Candidate>,
    pub baselines: Baselines,
}

#[derive(Debug, Deserialize)]
pub(super) struct Candidate {
    pub leg: String,
    pub effort: Option<String>,
}

#[derive(Debug, Deserialize)]
pub(super) struct Baselines {
    pub static_v0: Option<String>,
}

#[derive(Deserialize)]
struct Card {
    evidence: Vec<Evidence>,
}

#[derive(Deserialize)]
struct Evidence {
    kind: String,
    verified: bool,
    // Other evidence kinds carry different reference shapes.
    #[serde(rename = "ref")]
    reference: serde_json::Value,
}

#[derive(Deserialize)]
struct Telemetry {
    rows: Vec<Attempt>,
}

pub(super) fn attempts(
    env: &BTreeMap<String, String>,
    file: Option<&Path>,
    since: DateTime<Utc>,
    now: DateTime<Utc>,
) -> Result<(Vec<Attempt>, usize), String> {
    if let Some(file) = file {
        let stream = File::open(file).map_err(|_| "cannot open attempt export")?;
        let rows = serde_json::from_reader(std::io::BufReader::new(stream))
            .map_err(|_| "invalid attempt export")?;
        return Ok((rows, 0));
    }

    let client = if env.contains_key("PG_TELEMETRY_TOKEN_FILE") {
        Client::with_token_file(env, "PG_TELEMETRY_TOKEN_FILE")?
    } else {
        Client::new(env)?
    };
    // Keeper accepts durations. Allow for clock differences before applying the UTC cutoff locally.
    let seconds = (now - since).num_seconds() + 60;
    let Telemetry { mut rows } = client
        .get::<Telemetry>(&format!("v1/attempts?scope=telemetry&since={seconds}"))
        .map_err(|error| {
            if error == "keeper returned HTTP 403" {
                format!("{error}. Set PG_TELEMETRY_TOKEN_FILE to an unscoped Keeper reader token")
            } else {
                error
            }
        })?;
    let cards: BTreeSet<_> = rows
        .iter()
        .filter(|row| {
            row.started_at >= since
                && row.route.as_ref().is_some_and(|route| !route.pin)
                && row
                    .task_class
                    .as_deref()
                    .is_some_and(|class| !class.is_empty())
                && row.exit_class == "ok"
        })
        .filter_map(|row| row.card_key.as_deref())
        .collect();
    let mut verification = BTreeMap::new();
    let mut unavailable = 0;
    for key in cards {
        let encoded: String = key.bytes().map(|b| format!("%{b:02X}")).collect();
        match client.get::<Card>(&format!("v1/show?fmt=json&key={encoded}")) {
            Ok(card) => {
                let ids: BTreeSet<_> = card
                    .evidence
                    .into_iter()
                    .filter(|record| record.kind == "run-record" && record.verified)
                    .filter_map(|record| record.reference["run_id"].as_str().map(str::to_owned))
                    .collect();
                verification.insert(key.to_owned(), ids);
            }
            Err(error)
                if matches!(
                    error.as_str(),
                    "keeper returned HTTP 403" | "keeper returned HTTP 404"
                ) =>
            {
                unavailable += 1;
            }
            Err(error) => return Err(format!("cannot read Keeper run verification: {error}")),
        }
    }
    for row in &mut rows {
        row.verified = row
            .card_key
            .as_ref()
            .and_then(|key| verification.get(key))
            .map(|ids| ids.contains(&row.attempt_id));
    }
    Ok((rows, unavailable))
}

#[derive(Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Identity<'a> {
    pub model: &'a str,
    pub harness: &'a str,
}

#[derive(Debug)]
struct BenchCell {
    effort: String,
    harness: String,
    score: Option<f64>,
    cost: Option<f64>,
    legacy: bool,
}

pub(super) struct Bench {
    pub source: &'static str,
    cells: BTreeMap<String, Vec<BenchCell>>,
}

impl Bench {
    pub fn open(path: &Path) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let current: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'view'
                AND name = 'catalog_benchmark_rows')",
            [],
            |row| row.get(0),
        )?;
        let (source, query) = if current {
            (
                "preferred",
                "SELECT model_id, effort, harness, score, cost_per_task_usd, legacy
                FROM catalog_benchmark_rows ORDER BY model_id, effort, harness",
            )
        } else {
            (
                "legacy",
                "SELECT model_id, coalesce(parameter_effort, ''), '',
                aa_intelligence, cost_per_task_usd, 1 FROM catalog_benchmarks
                JOIN catalog_models USING (model_id)",
            )
        };
        let mut statement = conn.prepare(query)?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                BenchCell {
                    effort: row.get(1)?,
                    harness: row.get(2)?,
                    score: row.get(3)?,
                    cost: row.get(4)?,
                    legacy: row.get(5)?,
                },
            ))
        })?;
        let mut cells: BTreeMap<_, Vec<_>> = BTreeMap::new();
        for row in rows {
            let (model, cell) = row?;
            cells.entry(model).or_default().push(cell);
        }
        Ok(Self { source, cells })
    }

    pub fn utility(&self, leg: &Candidate, identity: Option<&Identity<'_>>) -> Option<f64> {
        let effort = leg.effort.as_deref().unwrap_or("");
        let cell = if let Some(identity) = identity {
            let cells = self
                .cells
                .get(identity.model)
                .or_else(|| {
                    self.cells
                        .get(&format!("{}/{}", identity.harness, identity.model))
                })
                .or_else(|| self.cells.get(&leg.leg))?;
            cells
                .iter()
                .find(|cell| cell.effort == effort && cell.harness == identity.harness)
                .or_else(|| {
                    cells.iter().find(|cell| {
                        cell.legacy
                            && (leg.effort.is_none() || cell.effort == effort)
                            && (cell.harness.is_empty() || cell.harness == identity.harness)
                    })
                })?
        } else {
            // A leg id that is itself a catalog id can resolve without an observed model
            // only when the requested effort identifies a single compatible cell.
            let mut matching = self
                .cells
                .get(&leg.leg)?
                .iter()
                .filter(|cell| cell.effort == effort);
            let cell = matching.next()?;
            if matching.next().is_some() {
                return None;
            }
            cell
        };
        let score = cell.score?;
        let cost = cell.cost?;
        let utility = score - COST_WEIGHT * cost;
        (score >= 0.0 && cost >= 0.0 && utility.is_finite()).then_some(utility)
    }
}
