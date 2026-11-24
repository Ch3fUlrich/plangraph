//! Cancels the CI-FIX items of pull requests that closed, turned into a probe, or moved to a new head.
use super::github::Github;
use super::{load_tasks, save_tasks, Result, Task};
use crate::atomic;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;

/// A draft, a `CI probe:` pull request and a landing train (`land/*` branch) never get a CI-FIX item:
/// the part's own writer or the landing owner handles their failures.
pub(super) fn not_a_fix_lane_pr(pr: &Value) -> bool {
    pr["draft"].as_bool().unwrap_or(false)
        || pr["title"]
            .as_str()
            .is_some_and(|t| t.trim_start().to_ascii_lowercase().starts_with("ci probe:"))
        || pr["head"]["ref"]
            .as_str()
            .is_some_and(|branch| branch.starts_with("land/"))
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
        let keeper_key = task.keeper_key.clone().unwrap_or_else(|| task.key.clone());
        let done = !task.keeper_defined || cancel(&keeper_key, &reason).is_ok();
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
