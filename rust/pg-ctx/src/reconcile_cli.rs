//! File-only dry run, explicit router apply, and persistent hourly alert state.
use crate::{
    keeper_reconcile::{self, AlertState, Assessment},
    keeper_truth::RepositoryIndex,
    truth_process::run,
};
use serde::de::DeserializeOwned;
use std::{collections::BTreeMap, path::Path};

fn read<T: DeserializeOwned>(path: &str) -> Result<T, String> {
    serde_json::from_slice(&std::fs::read(path).map_err(|_| "input unavailable")?)
        .map_err(|_| "input JSON invalid".into())
}

pub fn execute(args: &[String]) -> Result<String, String> {
    if args.len() < 2 {
        return Err(
            "reconcile-merged SNAPSHOT INDEX [ASSESSMENTS] [--apply D-N] or [--monitor STATE NOW]"
                .into(),
        );
    }
    let snapshot: serde_json::Value = read(&args[0])?;
    let items = snapshot["items"]
        .as_array()
        .ok_or("snapshot items missing")?;
    let index: RepositoryIndex = read(&args[1])?;
    let mut tail = &args[2..];
    let assessments: BTreeMap<String, Assessment> =
        if tail.first().is_some_and(|a| !a.starts_with("--")) {
            let result = read(&tail[0])?;
            tail = &tail[1..];
            result
        } else {
            BTreeMap::new()
        };
    let rows = keeper_reconcile::propose(items, &index, &assessments)?;
    match tail {
        [] => {}
        [flag, decision] if flag == "--apply" => {
            keeper_reconcile::apply_batch(&rows, decision, |key, decision, families| {
                let mut argv = vec!["close-merged", key, decision];
                for family in families {
                    argv.extend(["--family", family]);
                }
                run(Path::new("."), "pg", &argv).map(|_| ())
            })?;
        }
        [flag, path, now] if flag == "--monitor" => {
            let now = now.parse().map_err(|_| "invalid monitor clock")?;
            let mut state: AlertState = if Path::new(path).exists() {
                read(path)?
            } else {
                AlertState::default()
            };
            let alert = state.tick(rows.len(), now);
            let bytes = serde_json::to_vec(&state).map_err(|_| "state encoding failed")?;
            let parent = Path::new(path).parent().ok_or("state directory missing")?;
            let mut temp =
                tempfile::NamedTempFile::new_in(parent).map_err(|_| "state unavailable")?;
            use std::io::Write;
            temp.write_all(&bytes).map_err(|_| "state unavailable")?;
            temp.as_file().sync_all().map_err(|_| "state unavailable")?;
            temp.persist(path).map_err(|_| "state unavailable")?;
            if alert {
                eprintln!(
                    "ROUTING ALERT: {} merged nonterminal items remain for more than a day",
                    rows.len()
                );
            }
        }
        _ => return Err("invalid reconcile options".into()),
    }
    Ok(keeper_reconcile::tsv(&rows))
}

pub fn monitor(args: &[String]) -> Result<String, String> {
    let [snapshot, repo, reference, state] = args else {
        return Err("monitor SNAPSHOT REPOSITORY DEFAULT-REF STATE".into());
    };
    let modified = std::fs::metadata(snapshot)
        .and_then(|m| m.modified())
        .map_err(|_| "monitor snapshot unavailable")?;
    if modified
        .elapsed()
        .map_err(|_| "snapshot clock invalid")?
        .as_secs()
        > 7200
    {
        return Err("monitor snapshot older than two hours; refresh it first".into());
    }
    let index = crate::keeper_truth::collect(Path::new(repo), reference, true)?;
    let file = tempfile::NamedTempFile::new().map_err(|_| "monitor index unavailable")?;
    serde_json::to_writer(file.as_file(), &index).map_err(|_| "monitor index unavailable")?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "monitor clock invalid")?
        .as_secs()
        .to_string();
    execute(&[
        snapshot.clone(),
        file.path().to_string_lossy().into_owned(),
        "--monitor".into(),
        state.clone(),
        now,
    ])
}
