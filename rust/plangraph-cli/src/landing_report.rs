use crate::feeder::Transport;
use chrono::NaiveDate;
use serde_json::{json, Value};
use std::{collections::BTreeMap, io};

fn duration(row: &Value) -> Option<i64> {
    row["ended_at_ms"]
        .as_i64()?
        .checked_sub(row["started_at_ms"].as_i64()?)
        .filter(|ms| *ms >= 0)
}

fn slowest(rows: &[Value]) -> Option<Value> {
    let row = rows
        .iter()
        .filter_map(|row| Some((duration(row)?, row)))
        .max_by_key(|(elapsed, _)| *elapsed)?;
    Some(json!({
        "stage": row.1["stage"], "batch_key": row.1["batch_key"],
        "duration_ms": row.0, "minutes": row.0 as f64 / 60_000.0,
    }))
}

fn green_main(data: &Value, rows: &[Value]) -> Option<i64> {
    let merged = data["train"]["merge"].as_str()?;
    let jobs = data["jobs"].as_array()?;
    let attempts: Vec<_> = jobs
        .iter()
        .filter(|job| {
            job["head"] == merged
                && job["name"] == "ci-gate"
                && job["conclusion"] == "success"
                && job["run_conclusion"] == "success"
        })
        .filter_map(|job| {
            job["run_id"]
                .as_u64()?
                .checked_mul(1000)?
                .checked_add(job["attempt"].as_u64()?)
        })
        .collect();
    rows.iter()
        .filter(|row| {
            row["stage"] == "ci"
                && row["head"] == merged
                && row["attempt"]
                    .as_u64()
                    .is_some_and(|attempt| attempts.contains(&attempt))
        })
        .filter_map(|row| row["ended_at_ms"].as_i64())
        .min()
}

fn batch(data: &Value) -> Result<Value, String> {
    let rows = data["stages"]
        .as_array()
        .ok_or("timeline has no stage rows")?;
    let accept = data["accepts"]
        .as_array()
        .filter(|times| !times.is_empty())
        .and_then(|times| {
            times
                .iter()
                .map(Value::as_i64)
                .collect::<Option<Vec<_>>>()?
                .into_iter()
                .max()
        });
    let green = green_main(data, rows);
    let elapsed = accept
        .zip(green)
        .and_then(|(start, end)| end.checked_sub(start))
        .filter(|ms| *ms >= 0);
    let merge = rows
        .iter()
        .filter(|row| row["stage"] == "merge")
        .filter_map(|row| row["ended_at_ms"].as_i64())
        .max();
    let mut latest = BTreeMap::new();
    for row in rows {
        if let (Some(stage), Some(started), Some(ms)) = (
            row["stage"].as_str(),
            row["started_at_ms"].as_i64(),
            duration(row),
        ) {
            latest.insert(stage, (started, ms));
        }
    }
    let mut stages: Vec<_> = latest.into_iter().collect();
    stages.sort_by_key(|(_, (started, _))| *started);
    let mut summary = stages
        .into_iter()
        .map(|(stage, (_, ms))| format!("{stage}={:.1}m", ms as f64 / 60_000.0))
        .collect::<Vec<_>>()
        .join(" ");
    if let Some(ms) = elapsed {
        summary.push_str(&format!(" accept_to_green={:.1}m", ms as f64 / 60_000.0));
    }
    Ok(json!({
        "stages": rows, "jobs": data["jobs"], "slowest": slowest(rows), "summary": summary.trim(),
        "accept_to_green_ms": elapsed,
        "milestones": {"accepted_at_ms": accept, "merged_at_ms": merge, "green_main_at_ms": green},
    }))
}

pub(super) fn run(keeper: &dyn Transport, args: &[String]) -> Result<Value, String> {
    let (mut query, day, flags) = match args {
        [repo, mode, date, flags @ ..] if mode == "--day" => {
            let start = NaiveDate::parse_from_str(date, "%Y-%m-%d")
                .ok()
                .and_then(|date| date.and_hms_opt(0, 0, 0))
                .ok_or("landing report day must be YYYY-MM-DD")?
                .and_utc()
                .timestamp_millis();
            (
                json!({"action":"stage_day", "repo":repo, "start":start}),
                Some(date),
                flags,
            )
        }
        [repo, batch, flags @ ..] => (
            json!({"action":"timeline","repo":repo,"batch_key":batch}),
            None,
            flags,
        ),
        _ => {
            return Err("usage: plangraph landing report OWNER/REPO BATCH|--day YYYY-MM-DD".into())
        }
    };
    let data = match flags {
        [] => keeper.call("land", Some(query.take()))?,
        [flag] if flag == "--fixture" => serde_json::from_reader(io::stdin().lock())
            .map_err(|_| "invalid landing report fixture")?,
        _ => return Err("unknown landing report option".into()),
    };
    match day {
        None => batch(&data),
        Some(date) => {
            let selected = data["stages"]
                .as_array()
                .ok_or("daily report has no stage rows")?;
            let slow = slowest(selected);
            let summary = slow.as_ref().map(|row| {
                format!(
                    "{} {:.1} minutes",
                    row["stage"].as_str().unwrap_or("unknown"),
                    row["minutes"].as_f64().unwrap_or_default()
                )
            });
            // The operator's daily line also carries how many deploy entries are not live (D-6790): read from
            // the per-host ledgers, `unknown` when no host has reported, never zero by omission.
            let deploy = crate::deploy_manifest::report::digest_line_local();
            let summary = match summary {
                Some(stage) => format!("{stage}; {deploy}"),
                None => deploy.clone(),
            };
            Ok(json!({"day": date, "slowest": slow, "deploy_not_live": deploy, "summary": summary}))
        }
    }
}
