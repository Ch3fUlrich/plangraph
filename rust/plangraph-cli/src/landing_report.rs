use crate::feeder::Transport;
use crate::land::timeline;
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

/// The critical-path report over the last `n` landed trains: per train the wall time, bottleneck and idle
/// time, then the stage with the most critical-path time. `fixture` is `{"trains":[{"batch_key","timeline"}]}`
/// in the shape the keeper returns; without it the trains come from the keeper's `recent` and `timeline`.
fn last_trains(
    keeper: &dyn Transport,
    repo: &str,
    n: &str,
    fixture: bool,
) -> Result<Value, String> {
    let n: usize = n
        .parse()
        .ok()
        .filter(|n| (1..=100).contains(n))
        .ok_or("landing report --last needs a number from 1 to 100")?;
    let trains: Vec<(String, Value)> = if fixture {
        let data: Value = serde_json::from_reader(io::stdin().lock())
            .map_err(|_| "invalid landing report fixture")?;
        data["trains"]
            .as_array()
            .ok_or("fixture has no trains")?
            .iter()
            .map(|t| {
                Ok((
                    t["batch_key"]
                        .as_str()
                        .ok_or("train without batch_key")?
                        .to_owned(),
                    t["timeline"].clone(),
                ))
            })
            .collect::<Result<_, &str>>()?
    } else {
        let recent = keeper.call(
            "land",
            Some(json!({"action":"recent","repo":repo,"limit":n})),
        )?;
        recent["trains"]
            .as_array()
            .ok_or("keeper reply has no trains")?
            .iter()
            .map(|t| {
                let key = t["batch_key"].as_str().ok_or("train without batch_key")?;
                let timeline = keeper.call(
                    "land",
                    Some(json!({"action":"timeline","repo":repo,"batch_key":key})),
                )?;
                Ok((key.to_owned(), timeline))
            })
            .collect::<Result<_, String>>()?
    };
    let stages: Vec<_> = trains
        .iter()
        .flat_map(|(key, timeline)| timeline::from_keeper(key, timeline))
        .collect();
    // The keeper's roster, newest landing first: a train with no telemetry stays in the report as incomplete.
    let roster: Vec<String> = trains.iter().map(|(key, _)| key.clone()).collect();
    let report = timeline::report_roster(&roster, &stages, n);
    Ok(json!({
        "trains": report.trains.iter().map(|t| json!({
            "subject": t.subject, "wall_secs": t.wall_secs, "post_merge_secs": t.post_merge_secs,
            "bottleneck": t.bottleneck,
            "idle_secs": t.idle_secs, "critical_path": t.critical_path, "missing": t.missing,
        })).collect::<Vec<_>>(),
        "top_stages": report.top_stages,
        "lines": timeline::render(&report),
    }))
}

pub(super) fn run(keeper: &dyn Transport, args: &[String]) -> Result<Value, String> {
    if let [repo, mode, n, flags @ ..] = args {
        if mode == "--last" {
            let fixture = match flags {
                [] => false,
                [flag] if flag == "--fixture" => true,
                _ => return Err("unknown landing report option".into()),
            };
            return last_trains(keeper, repo, n, fixture);
        }
    }
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
            return Err("usage: plangraph landing report OWNER/REPO BATCH|--day YYYY-MM-DD|--last N [--fixture]".into())
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
            Ok(json!({"day": date, "slowest": slow, "summary": summary}))
        }
    }
}
