//! Cancels the CI-FIX items of pull requests that closed, turned into a probe, or moved to a new head.
use super::github::Github;
use super::{load_tasks, save_tasks, Result, Task};
use crate::atomic;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;

/// Where the failures of a pull request go.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Lane {
    /// A CI-FIX item in the keeper (or the batch writer that owns the pull request).
    Fix,
    /// A landing train (`land/*` branch): only the writer recorded for it hears of a failure.
    Owner,
    /// A draft or `CI probe:` pull request: its own writer handles it.
    None,
}

pub(super) fn lane(pr: &Value) -> Lane {
    if pr["draft"].as_bool().unwrap_or(false)
        || pr["title"]
            .as_str()
            .is_some_and(|t| t.trim_start().to_ascii_lowercase().starts_with("ci probe:"))
    {
        Lane::None
    } else if pr["head"]["ref"]
        .as_str()
        .is_some_and(|branch| branch.starts_with("land/"))
    {
        Lane::Owner
    } else {
        Lane::Fix
    }
}

/// True when no CI-FIX item may exist for the pull request.
pub(super) fn not_a_fix_lane_pr(pr: &Value) -> bool {
    lane(pr) != Lane::Fix
}

/// The head commit of the run that filed `task`: stored on new rows, read from the run for rows
/// written before the head was stored. `None` when GitHub cannot say; the row then stays.
fn failing_sha(gh: &mut impl Github, task: &Task) -> Option<String> {
    task.sha.clone().or_else(|| {
        gh.value(&format!("actions/runs/{}", task.run_id)).ok()?["head_sha"]
            .as_str()
            .map(str::to_owned)
    })
}

/// The keeper keys a row can have an item under: the delivered one, else the lane key and its
/// successor lane for the failing run (a create whose reply was lost leaves `keeper_defined` false).
fn keeper_keys(task: &Task) -> Vec<String> {
    match task.keeper_defined {
        true => vec![task.keeper_key.clone().unwrap_or_else(|| task.key.clone())],
        false => {
            let mut keys: Vec<String> = task.keeper_key.iter().cloned().collect();
            for key in [task.key.clone(), format!("{}-R{}", task.key, task.run_id)] {
                if !keys.contains(&key) {
                    keys.push(key);
                }
            }
            keys
        }
    }
}

/// Cancels every tracked CI-FIX item whose pull request is no longer a live fix-lane pull request or
/// whose head is no longer the failing run's head, then forgets it. `cancel(keeper_key, reason)` failures
/// leave the item for the next pass.
pub(super) fn sweep(
    gh: &mut impl Github,
    dir: &Path,
    dry: bool,
    mut cancel: impl FnMut(&str, &str) -> Result<()>,
) -> Result<Vec<String>> {
    let path = dir.join("tasks.jsonl");
    if !path.exists() {
        return Ok(Vec::new());
    }
    let _lock = if dry {
        None
    } else {
        Some(atomic::lock(&dir.join("scan")).map_err(|e| e.to_string())?)
    };
    let mut tasks = load_tasks(&path)?;
    if tasks.values().all(|t| t.pr.is_none()) {
        return Ok(Vec::new());
    }
    let mut live = BTreeMap::new();
    for pr in gh.pages("pulls?state=open&per_page=100", None)? {
        if !not_a_fix_lane_pr(&pr) {
            if let (Some(number), Some(sha)) = (pr["number"].as_u64(), pr["head"]["sha"].as_str()) {
                live.insert(number, sha.to_owned());
            }
        }
    }
    let mut output = Vec::new();
    let mut changed = false;
    for task in tasks.clone().into_values() {
        let Some(pr) = task.pr else { continue };
        let reason = match live.get(&pr) {
            None => {
                format!("pull request #{pr} is closed or is a draft, CI probe or landing train")
            }
            Some(now) => match failing_sha(gh, &task) {
                Some(then) if &then != now => {
                    format!("pull request #{pr} head moved on from the failing run's head")
                }
                _ => continue,
            },
        };
        output.push(format!(
            "{}cancel: {} ({reason})",
            if dry { "would " } else { "" },
            task.key
        ));
        if dry {
            continue;
        }
        // Every key the item can live under is closed (a missing or finished item counts as closed), so
        // an item whose creation reply was lost is not left open behind a deleted row.
        let closed: Vec<bool> = keeper_keys(&task)
            .iter()
            .map(|key| cancel(key, &reason).is_ok())
            .collect();
        let done = closed.iter().all(|closed| *closed);
        if done {
            tasks.remove(&task.key);
            changed = true;
        } else {
            output.push(format!("cancel pending: {}", task.key));
        }
    }
    if changed {
        save_tasks(&path, &tasks)?;
    }
    Ok(output)
}
