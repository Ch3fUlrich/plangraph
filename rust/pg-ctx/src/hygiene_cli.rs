use crate::keeper_hygiene::{decision_rows, lint, validate, Disposition};
use serde::de::DeserializeOwned;
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

fn json<T: DeserializeOwned>(path: &str) -> Result<T, String> {
    serde_json::from_slice(&std::fs::read(path).map_err(|_| "input unavailable")?)
        .map_err(|_| "invalid JSON input".into())
}

pub fn execute(args: &[String]) -> Result<String, String> {
    match args {
        [verb, snapshot, decisions, now] if verb == "lint" => {
            let snapshot: Value = json(snapshot)?;
            let items = snapshot["items"].as_array().ok_or("snapshot items missing")?;
            let rows = decision_rows(&std::fs::read_to_string(decisions).map_err(|_| "decisions unavailable")?);
            let now = now.parse().map_err(|_| "invalid clock")?;
            serde_json::to_string_pretty(&lint(items, &rows.into_keys().collect(), now)?)
                .map_err(|_| "report encoding failed".into())
        },
        [verb, snapshot, decisions, output] if verb == "lint-now" => {
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
                .map_err(|_| "invalid clock")?.as_secs();
            let report = execute(&["lint".into(), snapshot.clone(), decisions.clone(), now.to_string()])?;
            let parent = Path::new(output).parent().ok_or("output directory missing")?;
            let mut file = tempfile::NamedTempFile::new_in(parent).map_err(|_| "output unavailable")?;
            use std::io::Write;
            file.write_all(report.as_bytes()).map_err(|_| "output unavailable")?;
            file.as_file().sync_all().map_err(|_| "output unavailable")?;
            file.persist(output).map_err(|_| "output unavailable")?;
            Ok(report)
        },
        [verb, snapshot, dispositions, repos, decisions, now, decision] if verb == "cancel-check" => {
            let snapshot: Value = json(snapshot)?;
            let items = snapshot["items"].as_array().ok_or("snapshot items missing")?;
            let dispositions: Vec<Disposition> = json(dispositions)?;
            let repos: BTreeSet<String> = json(repos)?;
            let rows = decision_rows(&std::fs::read_to_string(decisions).map_err(|_| "decisions unavailable")?);
            let cells = rows.get(decision).ok_or("routing decision missing")?;
            if cells.len() != 1 { return Err("ambiguous routing decision".into()); }
            let keys = cells[0].strip_prefix("CANCEL ").ok_or("routing decision is not CANCEL")?
                .split(';').next().unwrap_or("").split_whitespace().collect::<BTreeSet<_>>();
            let now = now.parse().map_err(|_| "invalid clock")?;
            let mut seen = BTreeSet::new();
            let mut approved = BTreeMap::new();
            for disposition in dispositions {
                if !seen.insert(disposition.key.clone()) || !keys.contains(disposition.key.as_str()) {
                    return Err("duplicate or unrouted cancellation key".into());
                }
                let matches: Vec<_> = items.iter().filter(|i| i["key"].as_str() == Some(&disposition.key)).collect();
                if matches.len() != 1 { return Err("item missing or ambiguous".into()); }
                validate(matches[0], &disposition, &repos, now)?;
                approved.insert(disposition.key.clone(), disposition);
            }
            serde_json::to_string_pretty(&serde_json::json!({"decision_id":decision,"dispositions":approved}))
                .map_err(|_| "report encoding failed".into())
        },
        _ => Err("usage: pg-ctx lint SNAPSHOT DECISIONS UNIX | lint-now SNAPSHOT DECISIONS OUTPUT | cancel-check SNAPSHOT DISPOSITIONS REPOS DECISIONS UNIX D-ID".into()),
    }
}
