//! One immutable JSONL row per terminal native writer attempt; daily totals preserve unknowns.
use serde_json::{json, Value};
use std::{collections::BTreeMap, fs, io::Write, path::Path};

const METRICS: &[&str] = &[
    "tokens",
    "wall_minutes",
    "items_given",
    "items_done",
    "changed_lines",
    "review_rounds",
    "high_held",
    "med_held",
];

pub(crate) fn tokens(log: &str) -> Result<Option<u64>, String> {
    let mut text_total = None::<u64>;
    let mut json_total = None::<u64>;
    let mut pending = false;
    for line in log.lines() {
        let line = line.trim();
        let text = if pending {
            pending = false;
            Some(line)
        } else if line == "tokens used" {
            pending = true;
            None
        } else {
            line.strip_prefix("tokens used: ")
        };
        if let Some(text) = text {
            let count = text
                .replace(',', "")
                .parse::<u64>()
                .map_err(|_| "invalid tokens used count")?;
            text_total = Some(
                text_total
                    .unwrap_or(0)
                    .checked_add(count)
                    .ok_or("token overflow")?,
            );
        }
        if let Ok(event) = serde_json::from_str::<Value>(line) {
            if event["type"] == "turn.completed" {
                let usage = &event["usage"];
                let count = usage["input_tokens"]
                    .as_u64()
                    .ok_or("missing input tokens")?
                    .checked_add(
                        usage["output_tokens"]
                            .as_u64()
                            .ok_or("missing output tokens")?,
                    )
                    .ok_or("token overflow")?;
                json_total = Some(
                    json_total
                        .unwrap_or(0)
                        .checked_add(count)
                        .ok_or("token overflow")?,
                );
            }
        }
    }
    if pending {
        return Err("missing tokens used count".into());
    }
    // Human summaries and JSON usage describe the same turns; never add both.
    if text_total.is_some() && json_total.is_some() && text_total != json_total {
        return Err("text and JSON token totals disagree".into());
    }
    Ok(text_total.or(json_total))
}

pub(crate) fn append(directory: &Path, row: &Value) -> Result<(), String> {
    let _lock = pg_worker_core::lockfile::try_lock(&directory.join("ledger.lock"))
        .map_err(|e| e.to_string())?
        .ok_or("writer ledger busy")?;
    let path = directory.join("ledger.jsonl");
    let text = format!("{row}\n");
    let mut existing = match fs::read_to_string(&path) {
        Ok(existing) => existing,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e.to_string()),
    };
    let rows = parse_rows(&existing)?;
    let number = resume_number(row)?;
    if let Some(previous) = rows
        .iter()
        .find(|previous| resume_number(previous) == Ok(number))
    {
        return if previous == row {
            Ok(())
        } else {
            Err("terminal writer ledger row changed".into())
        };
    }
    if rows
        .last()
        .is_some_and(|previous| resume_number(previous).is_ok_and(|prior| prior >= number))
    {
        return Err("terminal writer ledger order changed".into());
    }
    if !existing.is_empty() && !existing.ends_with('\n') {
        existing.push('\n');
    }
    existing.push_str(&text);
    // Reuse the existing private atomic writer so reports never see a partial row.
    pg_worker_core::run::record_io::atomic_write_private(&path, existing.as_bytes())
        .map_err(|e| e.to_string())
}

fn resume_number(row: &Value) -> Result<u64, String> {
    if !row.is_object() {
        return Err("invalid writer ledger row".into());
    }
    row.get("resume_number").map_or(Ok(0), |value| {
        value
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or_else(|| "invalid writer ledger resume number".into())
    })
}

fn parse_rows(text: &str) -> Result<Vec<Value>, String> {
    let mut last = None;
    text.lines()
        .map(|line| {
            let row =
                serde_json::from_str(line).map_err(|e| format!("invalid writer ledger: {e}"))?;
            let number = resume_number(&row)?;
            if last.is_some_and(|prior| prior >= number) {
                return Err("invalid writer ledger order".into());
            }
            last = Some(number);
            Ok(row)
        })
        .collect()
}

pub(crate) fn report(args: Vec<String>, out: &mut dyn Write) -> Result<(), String> {
    if args.len() != 1 {
        return Err("ledger-report requires a runs directory".into());
    }
    let mut days = BTreeMap::<String, Value>::new();
    for entry in fs::read_dir(&args[0]).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        if !entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            continue;
        }
        let path = entry.path().join("ledger.jsonl");
        let text = match fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e.to_string()),
        };
        let rows = parse_rows(&text)?;
        if rows.is_empty() {
            return Err(format!("invalid ledger: {}", path.display()));
        }
        for row in rows {
            report_row(&mut days, &row)?;
        }
    }
    for day in days.values() {
        writeln!(out, "{day}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn report_row(days: &mut BTreeMap<String, Value>, row: &Value) -> Result<(), String> {
    let ended = row["ended"]
        .as_f64()
        .filter(|n| n.is_finite() && *n >= 0.0)
        .ok_or("invalid ledger end time")?;
    let date = chrono::DateTime::from_timestamp(ended as i64, 0)
        .ok_or("ledger end time out of range")?
        .format("%Y-%m-%d")
        .to_string();
    let day = days
        .entry(date.clone())
        .or_insert_with(|| json!({"day":date,"runs":0}));
    day["runs"] = json!(day["runs"].as_u64().unwrap() + 1);
    for metric in METRICS {
        let unknown = format!("{metric}_unknown");
        let missing = day[&unknown].as_u64().unwrap_or(0);
        if row[*metric].is_null() {
            if day[*metric].is_null() {
                day[*metric] = json!(0);
            }
            day[&unknown] = json!(missing + 1);
        } else if *metric == "wall_minutes" {
            let value = row[*metric]
                .as_f64()
                .filter(|n| n.is_finite() && *n >= 0.0)
                .ok_or("invalid ledger wall minutes")?;
            let total = day[*metric].as_f64().unwrap_or(0.0) + value;
            if !total.is_finite() {
                return Err("ledger wall minutes overflow".into());
            }
            day[*metric] = json!(total);
            day[&unknown] = json!(missing);
        } else {
            let value = row[*metric].as_u64().ok_or("invalid ledger count")?;
            day[*metric] = json!(day[*metric]
                .as_u64()
                .unwrap_or(0)
                .checked_add(value)
                .ok_or("ledger total overflow")?);
            day[&unknown] = json!(missing);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tokens_are_exact_and_malformed_or_conflicting_summaries_fail() {
        assert_eq!(
            tokens("tokens used\n9,007,199,254,740,993\n").unwrap(),
            Some(9_007_199_254_740_993)
        );
        assert_eq!(tokens("no usage\n").unwrap(), None);
        for log in ["tokens used\n", "tokens used: bad\n",
            "tokens used: 18446744073709551615\ntokens used: 1\n",
            "tokens used: 1\n{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":2,\"output_tokens\":1}}\n"] {
            assert!(tokens(log).is_err(), "{log}");
        }
        assert_eq!(tokens("tokens used: 3\n{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":2,\"output_tokens\":1}}\n").unwrap(), Some(3));
    }
    #[test]
    fn append_is_immutable_and_report_refuses_corrupt_rows() {
        let dir = tempfile::tempdir().unwrap();
        let run = dir.path().join("run-a");
        fs::create_dir(&run).unwrap();
        append(&run, &json!({"tokens":1})).unwrap();
        assert!(append(&run, &json!({"tokens":2})).is_err());
        fs::write(run.join("ledger.jsonl"), "broken\n").unwrap();
        assert!(report(vec![dir.path().to_str().unwrap().into()], &mut Vec::new()).is_err());
    }
}
