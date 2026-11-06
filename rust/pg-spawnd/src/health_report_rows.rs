//! Bounded, read-only compatibility reader. Corrections precede window selection.
use crate::health_report::ReportError;
use serde_json::Value;
use std::{collections::BTreeMap, io::Read, path::Path};

pub(crate) fn read_rows(state: &Path) -> Result<Vec<Value>, ReportError> {
    let dir = state.join("evidence");
    let single = dir.join("rows.jsonl");
    let mut monthly = Vec::new();
    if dir.exists() {
        regular(&dir, true)?;
        for entry in std::fs::read_dir(&dir)? {
            let path = entry?.path();
            let name = path.file_name().unwrap().to_string_lossy();
            if name.starts_with("outcomes-") && name.ends_with(".jsonl") {
                monthly.push(path);
            }
        }
    }
    monthly.sort();
    if single.exists() {
        if !monthly.is_empty() {
            return Err(ReportError::InvalidRow);
        }
        monthly.push(single);
    }
    let mut rows = Vec::new();
    for path in monthly {
        regular(&path, false)?;
        let mut bytes = Vec::new();
        std::fs::File::open(path)?
            .take(16 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() > 16 * 1024 * 1024 {
            return Err(ReportError::InvalidRow);
        }
        for line in bytes.split_inclusive(|b| *b == b'\n') {
            if line.len() > 65536 {
                return Err(ReportError::InvalidRow);
            }
            match serde_json::from_slice::<Value>(line) {
                Ok(v) => rows.push(if v["version"] == 1 {
                    v["data"]["row"].clone()
                } else {
                    v
                }),
                Err(_) if line.last() != Some(&b'\n') => (),
                Err(_) => return Err(ReportError::InvalidRow),
            }
        }
    }
    Ok(rows)
}
pub(crate) fn regular(path: &Path, directory: bool) -> Result<(), ReportError> {
    match std::fs::symlink_metadata(path) {
        Ok(m)
            if !m.file_type().is_symlink() && if directory { m.is_dir() } else { m.is_file() } =>
        {
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        _ => Err(ReportError::Io),
    }
}
pub(crate) fn corrected(records: &[Value], evidence: &[Value]) -> Vec<Value> {
    let mut rows = BTreeMap::new();
    for record in records {
        for (index, attempt) in record["attempts"]
            .as_array()
            .into_iter()
            .flatten()
            .enumerate()
        {
            let mut row = if attempt["outcome"].is_object() {
                attempt["outcome"].clone()
            } else {
                attempt.clone()
            };
            for field in ["run_id", "role", "plan", "override", "gate"] {
                if row[field].is_null() {
                    row[field] = record[field].clone();
                }
            }
            if row["attempt"].is_null() {
                row["attempt"] = index.into();
            }
            rows.insert((row["run_id"].to_string(), row["attempt"].to_string()), row);
        }
    }
    for row in evidence.iter().filter(|r| r.get("kind").is_none()) {
        rows.insert(
            (row["run_id"].to_string(), row["attempt"].to_string()),
            row.clone(),
        );
    }
    rows.into_values().collect()
}
pub(crate) fn observations(
    rows: &[Value],
    since: i64,
    until: i64,
) -> (Vec<Value>, Vec<Value>, Vec<Value>, usize) {
    let (mut parked, mut stale, mut episodes, mut invalid) =
        (Vec::new(), Vec::new(), BTreeMap::new(), 0);
    for row in rows.iter().filter(|r| r.get("kind").is_some()) {
        let (Some(ts), Some(leg), Some(kind)) = (
            row["ts"].as_i64(),
            row["leg_key"].as_str(),
            row["kind"].as_str(),
        ) else {
            invalid += 1;
            continue;
        };
        match kind {
            "omni_route_stale" | "omniroute_stale" if ts >= since && ts < until => {
                stale.push(row.clone())
            }
            "omni_route_parked" | "omniroute_parked" if row["until"].is_i64() => {
                if ts < until && row["until"].as_i64().unwrap() >= since {
                    parked.push(row.clone());
                }
            }
            "omni_route_parked" | "omniroute_parked" | "omni_route_seen" | "omniroute_seen" => {
                let (Some(first), Some(state)) =
                    (row["first_seen"].as_i64(), row["state"].as_str())
                else {
                    invalid += 1;
                    continue;
                };
                let entry = episodes
                    .entry((leg.to_owned(), state.to_owned(), first))
                    .or_insert((false, None));
                if kind.ends_with("parked") {
                    entry.0 = true;
                }
                if row["phase"] == "cleared" {
                    entry.1 = Some(ts);
                }
            }
            _ => (),
        }
    }
    let episodes = episodes.into_iter().filter_map(|((leg, state, first), (parked, end))| {
        let stop = end.unwrap_or(until);
        (parked && first < until && stop >= since && stop.saturating_sub(first) >= 86400)
            .then(|| serde_json::json!({"leg_key":leg,"state":state,"first_seen":first,"cleared":end}))
    }).collect();
    parked.sort_by_key(Value::to_string);
    parked.dedup();
    stale.sort_by_key(Value::to_string);
    (parked, episodes, stale, invalid)
}
#[cfg(test)]
#[path = "health_report_rows_tests.rs"]
mod tests;
