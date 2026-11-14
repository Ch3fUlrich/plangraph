#[cfg(test)]
#[path = "usage_census_tests.rs"]
mod tests;
// Read-only census. Missing role, trigger or fork provenance stays visible.
use chrono::{DateTime, Duration, Utc};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs,
    io::{BufRead, BufReader},
    path::Path,
};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const ROLES: [&str; 9] = [
    "router",
    "replan",
    "L1-build",
    "L1-other",
    "L2",
    "subagent-fork",
    "subagent-fresh",
    "subagent-unknown",
    "unknown",
];
const TRIGGERS: [&str; 5] = [
    "msg-in",
    "coordination-tool-loop",
    "beat",
    "work",
    "unknown",
];
fn time(value: &str) -> Result<DateTime<Utc>> {
    Ok(DateTime::parse_from_rfc3339(value)?.with_timezone(&Utc))
}
#[derive(Default)]
struct Counts {
    weighted: f64,
    turns: u64,
    triggers: BTreeMap<String, u64>,
}
struct Turn {
    at: DateTime<Utc>,
    role: String,
    trigger: String,
    weighted: f64,
}
fn role(name: &str) -> &str {
    let name = name.to_ascii_lowercase();
    if name.starts_with("routing") || name.starts_with("router") {
        "router"
    } else if name.starts_with("replan") || name.starts_with("plangraph-replan") {
        "replan"
    } else if name.starts_with("build-l1") || name.starts_with("l1-build") {
        "L1-build"
    } else if name.starts_with("l1") {
        "L1-other"
    } else if name.starts_with("l2") || name.starts_with("build-l2") {
        "L2"
    } else {
        "unknown"
    }
}
fn read(
    path: &Path,
    output: &mut HashMap<(String, String), Turn>,
    malformed: &mut u64,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Result<()> {
    let mut rows: HashMap<(String, String), Turn> = HashMap::new();
    let sidechain = path.components().any(|s| s.as_os_str() == "subagents");
    let mut title = "unknown".to_string();
    let mut spawn = "unknown".to_string();
    let mut trigger = "unknown".to_string();
    let identity = path.to_string_lossy().into_owned();
    for line in BufReader::new(fs::File::open(path)?).lines() {
        let record = match serde_json::from_str::<Value>(&line?) {
            Ok(Value::Object(v)) => Value::Object(v),
            _ => {
                *malformed += 1;
                continue;
            }
        };
        if let Some(name) = record["customTitle"]
            .as_str()
            .or(record["agentName"].as_str())
        {
            title = role(name).into();
        }
        if let Some(kind) = record["spawn_kind"].as_str() {
            spawn = kind.into();
        }
        if record["type"] == "user" {
            trigger = "unknown".into();
        }
        if let Some(value) = record["trigger"].as_str() {
            trigger = if TRIGGERS.contains(&value) {
                value
            } else {
                "unknown"
            }
            .into();
        }
        if record["type"] != "assistant" {
            continue;
        }
        let parsed = (|| -> Result<_> {
            let msg = &record["message"];
            let id = msg["id"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or("missing request id")?;
            let at = time(record["timestamp"].as_str().ok_or("missing timestamp")?)?;
            if at < start || at >= end {
                return Ok(());
            }
            let usage = msg["usage"].as_object().ok_or("missing usage")?;
            let mut weighted = 0.0;
            for (field, weight) in [
                ("input_tokens", 1.0),
                ("output_tokens", 1.0),
                ("cache_creation_input_tokens", 1.0),
                ("cache_read_input_tokens", 0.1),
            ] {
                let n = usage
                    .get(field)
                    .map_or(Some(0.0), Value::as_f64)
                    .ok_or("invalid usage")?;
                if n < 0.0 || !n.is_finite() {
                    return Err("invalid usage".into());
                }
                weighted += n * weight;
            }
            if !weighted.is_finite() {
                return Err("usage overflow".into());
            }
            let key = (identity.clone(), id.to_string());
            let prior = rows.get(&key);
            let attributed = if trigger == "unknown" {
                prior.map(|t| t.trigger.as_str()).unwrap_or(&trigger)
            } else {
                &trigger
            };
            let kind = if sidechain || record["isSidechain"] == true {
                match spawn.as_str() {
                    "fork" => "subagent-fork",
                    "fresh" => "subagent-fresh",
                    _ => "subagent-unknown",
                }
            } else {
                &title
            };
            let turn = Turn {
                at,
                role: kind.into(),
                trigger: attributed.into(),
                weighted,
            };
            rows.insert(key, turn);
            Ok(())
        })();
        if parsed.is_err() {
            *malformed += 1;
        }
    }
    // Titles may be recorded after a request; they describe the whole session.
    if !sidechain {
        for (_, turn) in rows.iter_mut().filter(|((file, _), _)| file == &identity) {
            if !turn.role.starts_with("subagent-") {
                turn.role.clone_from(&title);
            }
        }
    }
    output.extend(rows);
    Ok(())
}
fn collect(
    root: &Path,
    rows: &mut HashMap<(String, String), Turn>,
    malformed: &mut u64,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Result<()> {
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        if kind.is_dir() {
            collect(&entry.path(), rows, malformed, start, end)?;
        } else if kind.is_file()
            && entry.path().extension().is_some_and(|s| s == "jsonl")
            && DateTime::<Utc>::from(entry.metadata()?.modified()?) >= start
        {
            read(&entry.path(), rows, malformed, start, end)?;
        }
    }
    Ok(())
}
/// Weights follow claude_usage_policy.json: cached reads count as 0.1 token.
/// Counts are rates over the same window, not claims of per-PR attribution.
pub fn census(root: &Path, since: &str, until: &str, prs: &[Value]) -> Result<Value> {
    let (start, end) = (time(since)?, time(until)?);
    if end <= start {
        return Err("invalid census window".into());
    }
    let mut rows = HashMap::new();
    let mut malformed = 0;
    collect(root, &mut rows, &mut malformed, start, end)?;
    let mut daily = BTreeMap::new();
    let mut day = start.date_naive();
    while day.and_hms_opt(0, 0, 0).ok_or("invalid date")?.and_utc() < end {
        daily.insert(day.to_string(), 0_u64);
        day = day.succ_opt().ok_or("date overflow")?;
    }
    let mut seen = HashSet::new();
    for pr in prs {
        let number = pr["number"].as_u64().ok_or("invalid PR number")?;
        let at = time(pr["mergedAt"].as_str().ok_or("invalid PR timestamp")?)?;
        if start <= at && at < end && seen.insert(number) {
            *daily
                .get_mut(&at.date_naive().to_string())
                .ok_or("missing day")? += 1;
        }
    }
    let mut counts: BTreeMap<_, Counts> = ROLES
        .iter()
        .map(|r| (r.to_string(), Counts::default()))
        .collect();
    for row in rows.values().filter(|r| start <= r.at && r.at < end) {
        let c = counts.get_mut(&row.role).ok_or("unknown role")?;
        c.turns += 1;
        c.weighted += row.weighted;
        *c.triggers.entry(row.trigger.clone()).or_default() += 1;
    }
    let roles: BTreeMap<_, _> = counts.into_iter().map(|(role,c)| {
        let shares: BTreeMap<_,_> = TRIGGERS.iter().map(|t| (*t, if c.turns == 0 { 0.0 }
            else { *c.triggers.get(*t).unwrap_or(&0) as f64 / c.turns as f64 })).collect();
        (role,json!({"turns":c.turns,"weighted_tokens":c.weighted,"trigger_share":shares,
            "weighted_tokens_per_merged_pr": if seen.is_empty() { None } else { Some(c.weighted / seen.len() as f64) }}))
    }).collect();
    let midnight = end
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .ok_or("invalid date")?
        .and_utc();
    let guard = if start > midnight - Duration::days(3) {
        "insufficient-window"
    } else if (1..=3).all(|n| {
        daily
            .get(&(midnight - Duration::days(n)).date_naive().to_string())
            .is_some_and(|v| *v < 58)
    }) {
        "revert-cut"
    } else {
        "ok"
    };
    Ok(
        json!({"since":start.to_rfc3339(),"until":end.to_rfc3339(),"roles":roles,
        "merged_prs":seen.len(),"merged_prs_per_day":daily,"throughput_guard":guard,
        "diagnostics":{"malformed_records":malformed},"attribution":"window rates; unknown provenance is not inferred"}),
    )
}
